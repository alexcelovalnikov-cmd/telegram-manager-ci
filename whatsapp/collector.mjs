import fs from 'node:fs'
import path from 'node:path'
import crypto from 'node:crypto'
import https from 'node:https'
import process from 'node:process'
import Database from 'better-sqlite3'
import QRCode from 'qrcode'
import pino from 'pino'
import { SocksProxyAgent } from 'socks-proxy-agent'
import makeWASocket, {
  Browsers,
  DisconnectReason,
  jidNormalizedUser,
  useMultiFileAuthState
} from '@whiskeysockets/baileys'

const root = process.env.TM_WHATSAPP_STATE_DIR || '/state'
const authDir = path.join(root, 'auth')
const dbPath = path.join(root, 'messages.sqlite')
const qrPath = path.join(root, 'login-qr.png')
const healthPath = path.join(root, 'health.json')
const backfillRequestPath = path.join(root, 'backfill-request.json')
const backfillResultPath = path.join(root, 'backfill-result.json')
const logger = pino({ level: process.env.TM_WHATSAPP_LOG_LEVEL || 'warn' })
const proxyUrl = process.env.TM_WHATSAPP_SOCKS_PROXY || ''
const proxyAgent = proxyUrl ? new SocksProxyAgent(proxyUrl) : undefined

const fetchLiveWaWebVersion = async () => {
  const body = await new Promise((resolve,reject) => {
    const req=https.get('https://web.whatsapp.com/sw.js',{
      agent:proxyAgent,
      headers:{'user-agent':'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36','sec-fetch-site':'none'}
    },(res)=>{
      if (!res.statusCode || res.statusCode < 200 || res.statusCode >= 300) {
        res.resume()
        reject(new Error(`wa_web_version_http_${res.statusCode || 0}`))
        return
      }
      let data=''
      res.setEncoding('utf8')
      res.on('data',(chunk)=>{ if (data.length < 1_000_000) data += chunk })
      res.on('end',()=>resolve(data))
    })
    req.setTimeout(30000,()=>req.destroy(new Error('wa_web_version_timeout')))
    req.on('error',reject)
  })
  const match=String(body).match(/\\?"client_revision\\?":\s*(\d+)/)
  if (!match?.[1]) throw new Error('whatsapp_live_web_version_unavailable')
  return [2,3000,Number(match[1])]
}

fs.mkdirSync(root, { recursive: true, mode: 0o700 })
fs.mkdirSync(authDir, { recursive: true, mode: 0o700 })
fs.chmodSync(root, 0o700)

const db = new Database(dbPath)
db.pragma('journal_mode = WAL')
db.pragma('synchronous = FULL')
db.pragma('busy_timeout = 5000')
db.pragma('foreign_keys = ON')
db.exec(`
CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chats(
  jid TEXT PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  search_name TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL,
  last_message_at TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  jid TEXT NOT NULL,
  message_id TEXT NOT NULL,
  date TEXT NOT NULL,
  sender_jid TEXT,
  sender_name TEXT,
  text TEXT NOT NULL DEFAULT '',
  search_text TEXT NOT NULL DEFAULT '',
  reply_to_message_id TEXT,
  from_me INTEGER NOT NULL DEFAULT 0,
  has_media INTEGER NOT NULL DEFAULT 0,
  media_type TEXT,
  is_deleted INTEGER NOT NULL DEFAULT 0,
  edited_at TEXT,
  content_token TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(jid,message_id)
);
CREATE INDEX IF NOT EXISTS messages_chat_date_idx ON messages(jid,date DESC,seq DESC);
CREATE INDEX IF NOT EXISTS messages_date_idx ON messages(date DESC,seq DESC);
CREATE INDEX IF NOT EXISTS messages_reply_idx ON messages(jid,reply_to_message_id);
CREATE TABLE IF NOT EXISTS identities(
  identity_key TEXT PRIMARY KEY,
  display_name TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS identity_aliases(
  alias TEXT PRIMARY KEY,
  identity_key TEXT NOT NULL REFERENCES identities(identity_key) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS identity_aliases_identity_idx ON identity_aliases(identity_key);
CREATE TABLE IF NOT EXISTS approval_identity_map(
  jid TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('approver_a','approver_b')),
  identity_key TEXT NOT NULL REFERENCES identities(identity_key),
  verified_source TEXT NOT NULL,
  verified_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(jid,role)
);
CREATE TABLE IF NOT EXISTS reaction_events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  jid TEXT NOT NULL,
  target_message_id TEXT NOT NULL,
  reaction_message_id TEXT NOT NULL,
  actor_identity_key TEXT NOT NULL REFERENCES identities(identity_key),
  actor_jid TEXT,
  actor_lid TEXT,
  actor_display_name TEXT NOT NULL DEFAULT '',
  reaction_value TEXT NOT NULL DEFAULT '',
  is_removed INTEGER NOT NULL DEFAULT 0,
  reacted_at TEXT NOT NULL,
  source TEXT NOT NULL,
  evidence_token TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(jid,reaction_message_id,evidence_token)
);
CREATE INDEX IF NOT EXISTS reaction_events_target_idx
  ON reaction_events(jid,target_message_id,reacted_at DESC,seq DESC);
CREATE INDEX IF NOT EXISTS reaction_events_actor_idx
  ON reaction_events(actor_identity_key,reacted_at DESC,seq DESC);
`)

const setMeta = db.prepare(`INSERT INTO meta(key,value) VALUES (?,?)
  ON CONFLICT(key) DO UPDATE SET value=excluded.value`)
const getMeta = db.prepare('SELECT value FROM meta WHERE key=?')
const upsertChatStmt = db.prepare(`
INSERT INTO chats(jid,name,search_name,kind,last_message_at,updated_at)
VALUES (@jid,@name,@search_name,@kind,@last_message_at,@updated_at)
ON CONFLICT(jid) DO UPDATE SET
 name=CASE WHEN excluded.name<>'' THEN excluded.name ELSE chats.name END,
 search_name=CASE WHEN excluded.name<>'' THEN excluded.search_name ELSE chats.search_name END,
 kind=excluded.kind,
 last_message_at=CASE
   WHEN chats.last_message_at IS NULL THEN excluded.last_message_at
   WHEN excluded.last_message_at IS NULL THEN chats.last_message_at
   WHEN excluded.last_message_at>chats.last_message_at THEN excluded.last_message_at
   ELSE chats.last_message_at END,
 updated_at=excluded.updated_at
`)
const upsertMessageStmt = db.prepare(`
INSERT INTO messages(jid,message_id,date,sender_jid,sender_name,text,search_text,reply_to_message_id,
 from_me,has_media,media_type,is_deleted,edited_at,content_token,updated_at)
VALUES (@jid,@message_id,@date,@sender_jid,@sender_name,@text,@search_text,@reply_to_message_id,
 @from_me,@has_media,@media_type,0,@edited_at,@content_token,@updated_at)
ON CONFLICT(jid,message_id) DO UPDATE SET
 date=excluded.date,sender_jid=excluded.sender_jid,sender_name=excluded.sender_name,
 text=excluded.text,search_text=excluded.search_text,reply_to_message_id=excluded.reply_to_message_id,
 from_me=excluded.from_me,has_media=excluded.has_media,media_type=excluded.media_type,
 is_deleted=0,edited_at=excluded.edited_at,content_token=excluded.content_token,updated_at=excluded.updated_at
`)
const deleteMessageStmt = db.prepare(`UPDATE messages SET is_deleted=1,updated_at=?,
 content_token=? WHERE jid=? AND message_id=?`)
const existingMessageStmt = db.prepare(`SELECT * FROM messages WHERE jid=? AND message_id=?`)
const oldestMessageStmt = db.prepare(`
SELECT message_id,date,sender_jid,from_me
FROM messages WHERE jid=? AND is_deleted=0 ORDER BY date ASC,seq ASC LIMIT 1
`)
const historyStatsStmt = db.prepare(`
SELECT count(*) AS n,min(date) AS oldest_date,max(date) AS newest_date
FROM messages WHERE jid=? AND is_deleted=0
`)
const identityForAliasStmt = db.prepare('SELECT identity_key FROM identity_aliases WHERE alias=?')
const upsertIdentityStmt = db.prepare(`
INSERT INTO identities(identity_key,display_name,created_at,updated_at)
VALUES (@identity_key,@display_name,@now,@now)
ON CONFLICT(identity_key) DO UPDATE SET
 display_name=CASE WHEN excluded.display_name<>'' THEN excluded.display_name ELSE identities.display_name END,
 updated_at=excluded.updated_at
`)
const upsertIdentityAliasStmt = db.prepare(`
INSERT INTO identity_aliases(alias,identity_key,kind,updated_at)
VALUES (@alias,@identity_key,@kind,@updated_at)
ON CONFLICT(alias) DO UPDATE SET
 identity_key=excluded.identity_key,kind=excluded.kind,updated_at=excluded.updated_at
`)
const mergeIdentityAliasesStmt = db.prepare(
  'UPDATE identity_aliases SET identity_key=?,updated_at=? WHERE identity_key=?'
)
const mergeReactionIdentityStmt = db.prepare(
  'UPDATE reaction_events SET actor_identity_key=?,updated_at=? WHERE actor_identity_key=?'
)
const mergeApprovalIdentityStmt = db.prepare(
  'UPDATE approval_identity_map SET identity_key=?,updated_at=? WHERE identity_key=?'
)
const deleteIdentityStmt = db.prepare('DELETE FROM identities WHERE identity_key=?')
const insertReactionStmt = db.prepare(`
INSERT OR IGNORE INTO reaction_events(
 jid,target_message_id,reaction_message_id,actor_identity_key,actor_jid,actor_lid,
 actor_display_name,reaction_value,is_removed,reacted_at,source,evidence_token,updated_at
) VALUES (
 @jid,@target_message_id,@reaction_message_id,@actor_identity_key,@actor_jid,@actor_lid,
 @actor_display_name,@reaction_value,@is_removed,@reacted_at,@source,@evidence_token,@updated_at
)
`)

const names = new Map()
let selfJid = getMeta.get('account_jid')?.value || null
let selfLid = getMeta.get('account_lid')?.value || null
const nowIso = () => new Date().toISOString()
const normalize = (value) => (value || '').toLocaleLowerCase('ru-RU').replaceAll('ё','е').replace(/\s+/g,' ').trim()

const atomicJson = (file, value) => {
  const tmp = file + '.tmp'
  fs.writeFileSync(tmp, JSON.stringify(value, null, 2), { mode: 0o600 })
  fs.renameSync(tmp, file)
}
const readBoundedJson = (file) => {
  try {
    const stat=fs.lstatSync(file)
    if (stat.isSymbolicLink() || stat.size > 65536) return null
    const value=JSON.parse(fs.readFileSync(file,'utf8'))
    return value && typeof value === 'object' && !Array.isArray(value) ? value : null
  } catch {
    return null
  }
}
const health = (extra={}) => {
  const status = {
    service: 'whatsapp-collector',
    status: getMeta.get('connection_status')?.value || 'starting',
    qr_ready: getMeta.get('qr_ready')?.value === 'true',
    account_jid: getMeta.get('account_jid')?.value || null,
    last_event_at: getMeta.get('last_event_at')?.value || null,
    last_success_at: getMeta.get('last_success_at')?.value || null,
    updated_at: nowIso(),
    ...extra
  }
  atomicJson(healthPath, status)
}
const setStatus = (status, extra={}) => {
  setMeta.run('connection_status', status)
  setMeta.run('last_event_at', nowIso())
  for (const [k,v] of Object.entries(extra)) {
    if (v !== undefined && v !== null) setMeta.run(k, String(v))
  }
  health()
}
const safeJid = (jid) => {
  if (!jid || typeof jid !== 'string') return null
  const value = jidNormalizedUser(jid)
  if (!value || value === 'status@broadcast' || value.endsWith('@broadcast') || value.endsWith('@newsletter')) return null
  return value
}
const chatKind = (jid) => jid.endsWith('@g.us') ? 'group' : 'private'
const identityAlias = (jid) => {
  const value=safeJid(jid)
  return value && (value.endsWith('@lid') || value.endsWith('@s.whatsapp.net')) ? value : null
}
const aliasKind = (alias) => alias?.endsWith('@lid') ? 'lid' : 'jid'
const dateIso = (value) => {
  let n = typeof value === 'bigint' ? Number(value) : Number(value || 0)
  if (!Number.isFinite(n) || n <= 0) return nowIso()
  if (n < 10_000_000_000) n *= 1000
  return new Date(n).toISOString()
}
const unwrap = (message) => {
  let current = message
  for (let i=0;i<6 && current;i++) {
    if (current.ephemeralMessage?.message) current = current.ephemeralMessage.message
    else if (current.viewOnceMessage?.message) current = current.viewOnceMessage.message
    else if (current.viewOnceMessageV2?.message) current = current.viewOnceMessageV2.message
    else if (current.documentWithCaptionMessage?.message) current = current.documentWithCaptionMessage.message
    else break
  }
  return current || {}
}
const mediaKind = (m) => {
  for (const key of ['imageMessage','videoMessage','audioMessage','documentMessage','stickerMessage','contactMessage','locationMessage']) {
    if (m[key]) return key.replace('Message','')
  }
  return null
}
const messageText = (m) => {
  if (typeof m.conversation === 'string') return m.conversation
  if (typeof m.extendedTextMessage?.text === 'string') return m.extendedTextMessage.text
  for (const key of ['imageMessage','videoMessage','documentMessage']) {
    if (typeof m[key]?.caption === 'string') return m[key].caption
  }
  if (typeof m.buttonsResponseMessage?.selectedDisplayText === 'string') return m.buttonsResponseMessage.selectedDisplayText
  if (typeof m.listResponseMessage?.title === 'string') return m.listResponseMessage.title
  return ''
}
const contextInfo = (m) =>
  m.extendedTextMessage?.contextInfo ||
  m.imageMessage?.contextInfo ||
  m.videoMessage?.contextInfo ||
  m.documentMessage?.contextInfo ||
  {}
const tokenFor = (row) => crypto.createHash('sha256').update(JSON.stringify([
  row.jid,row.message_id,row.date,row.sender_jid,row.sender_name,row.text,row.reply_to_message_id,
  row.from_me,row.has_media,row.media_type,row.is_deleted || 0,row.edited_at || null
])).digest('hex')
const chatName = (jid, fallback='') => names.get(jid) || fallback || jid.split('@')[0]

const rememberIdentity = db.transaction((rawAliases, displayName='') => {
  const aliases=[...new Set((rawAliases || []).map(identityAlias).filter(Boolean))].sort()
  if (!aliases.length) return null
  const existing=[...new Set(
    aliases.map(alias=>identityForAliasStmt.get(alias)?.identity_key).filter(Boolean)
  )].sort()
  const now=nowIso()
  const identityKey=existing[0] || ('wa:'+crypto.createHash('sha256').update(
    JSON.stringify(aliases)
  ).digest('hex').slice(0,32))
  upsertIdentityStmt.run({
    identity_key:identityKey,
    display_name:String(displayName || '').slice(0,300),
    now
  })
  for (const oldKey of existing.slice(1)) {
    mergeReactionIdentityStmt.run(identityKey,now,oldKey)
    mergeApprovalIdentityStmt.run(identityKey,now,oldKey)
    mergeIdentityAliasesStmt.run(identityKey,now,oldKey)
    deleteIdentityStmt.run(oldKey)
  }
  for (const alias of aliases) {
    upsertIdentityAliasStmt.run({
      alias,identity_key:identityKey,kind:aliasKind(alias),updated_at:now
    })
  }
  return {
    identity_key:identityKey,
    actor_jid:aliases.find(value=>value.endsWith('@s.whatsapp.net')) || null,
    actor_lid:aliases.find(value=>value.endsWith('@lid')) || null
  }
})
const reactionActorAliases = (key) => {
  const aliases=[
    key?.participantAlt,key?.remoteJidAlt,key?.participant,key?.remoteJid
  ]
  if (key?.fromMe) aliases.push(selfJid,selfLid)
  return aliases
}
const reactionTokenFor = (row) => crypto.createHash('sha256').update(JSON.stringify([
  row.jid,row.target_message_id,row.reaction_message_id,
  row.reaction_value,row.is_removed,row.reacted_at
])).digest('hex')

function upsertChat(jid, name='', last=null) {
  jid=safeJid(jid)
  if (!jid) return
  const n=(name || '').slice(0,300)
  if (n) names.set(jid,n)
  upsertChatStmt.run({
    jid,name:n,search_name:normalize(n),kind:chatKind(jid),
    last_message_at:last,updated_at:nowIso()
  })
}
function storeReaction(targetKey, reaction, source='live', senderFallback='', fallbackTimestamp=null) {
  const jid=safeJid(targetKey?.remoteJid)
  const targetMessageId=targetKey?.id
  const actorKey=reaction?.key || {}
  if (!jid || !targetMessageId) return false
  const aliasValues=reactionActorAliases(actorKey)
  const actorName=String(
    senderFallback ||
    aliasValues.map(identityAlias).filter(Boolean).map(alias=>names.get(alias)).find(Boolean) ||
    ''
  ).slice(0,300)
  const identity=rememberIdentity(aliasValues,actorName)
  if (!identity) return false
  const reactionValue=typeof reaction?.text === 'string' ? reaction.text.slice(0,64) : ''
  const reactedAt=dateIso(reaction?.senderTimestampMs || fallbackTimestamp)
  const rawReactionId=actorKey?.id ? String(actorKey.id).slice(0,200) : ''
  const reactionMessageId=rawReactionId || (
    'synthetic:'+crypto.createHash('sha256').update(JSON.stringify([
      jid,String(targetMessageId),identity.actor_jid,identity.actor_lid,reactedAt,reactionValue
    ])).digest('hex').slice(0,40)
  )
  const row={
    jid,
    target_message_id:String(targetMessageId).slice(0,200),
    reaction_message_id:reactionMessageId,
    actor_identity_key:identity.identity_key,
    actor_jid:identity.actor_jid,
    actor_lid:identity.actor_lid,
    actor_display_name:actorName,
    reaction_value:reactionValue,
    is_removed:reactionValue ? 0 : 1,
    reacted_at:reactedAt,
    source:String(source || 'unknown').slice(0,32),
    updated_at:nowIso()
  }
  row.evidence_token=reactionTokenFor(row)
  insertReactionStmt.run(row)
  upsertChat(jid,chatName(jid),reactedAt)
  setMeta.run('last_event_at',nowIso())
  return true
}
function storeReactionFromMessage(item, source='history') {
  const body=unwrap(item?.message || {})
  if (!body?.reactionMessage?.key) return false
  return storeReaction(
    body.reactionMessage.key,
    {...body.reactionMessage,key:item.key},
    source,
    item?.pushName || '',
    item?.messageTimestamp
  )
}
function storeAnyMessage(item, source='history') {
  if (storeReactionFromMessage(item,source)) return
  storeMessage(item)
}
function storeMessage(item, senderFallback='') {
  const jid=safeJid(item?.key?.remoteJid)
  const messageId=item?.key?.id
  if (!jid || !messageId) return
  const body=unwrap(item.message || {})
  const sender=safeJid(item?.key?.fromMe ? selfJid : (item?.key?.participant || item?.key?.remoteJid))
  const senderName=(item.pushName || names.get(sender) || senderFallback || '').slice(0,300)
  const text=messageText(body).slice(0,50000)
  const media=mediaKind(body)
  const ctx=contextInfo(body)
  const row={
    jid,message_id:String(messageId).slice(0,200),date:dateIso(item.messageTimestamp),
    sender_jid:sender,sender_name:senderName,text,search_text:normalize(text),
    reply_to_message_id:ctx?.stanzaId ? String(ctx.stanzaId).slice(0,200) : null,
    from_me:item?.key?.fromMe ? 1 : 0,has_media:media ? 1 : 0,media_type:media,
    edited_at:null,updated_at:nowIso()
  }
  row.content_token=tokenFor(row)
  upsertMessageStmt.run(row)
  upsertChat(jid, chatName(jid), row.date)
  setMeta.run('last_event_at',nowIso())
}
const storeBatch=db.transaction((items,source)=>{ for(const item of items) storeAnyMessage(item,source) })
function deleteMessage(key) {
  const jid=safeJid(key?.remoteJid)
  const id=key?.id
  if (!jid || !id) return
  const old=existingMessageStmt.get(jid,String(id))
  if (!old) return
  const token=tokenFor({...old,is_deleted:1})
  deleteMessageStmt.run(nowIso(),token,jid,String(id))
  setMeta.run('last_event_at',nowIso())
}
function rememberContact(contact) {
  const jid=safeJid(contact?.id || contact?.jid)
  const name=contact?.name || contact?.notify || contact?.verifiedName || ''
  const identity=rememberIdentity(
    [contact?.id,contact?.jid,contact?.lid,contact?.phoneNumber],
    name
  )
  for (const alias of [identity?.actor_jid,identity?.actor_lid].filter(Boolean)) {
    if (name) names.set(alias,String(name).slice(0,300))
  }
  if (!jid) return
  if (name) {
    names.set(jid,String(name).slice(0,300))
    upsertChat(jid,String(name),null)
  }
}
function rememberChat(chat) {
  const jid=safeJid(chat?.id)
  if (!jid) return
  const name=chat?.name || chat?.subject || names.get(jid) || ''
  upsertChat(jid,name,chat?.conversationTimestamp ? dateIso(chat.conversationTimestamp) : null)
}
async function connectOnce() {
  const { state, saveCreds } = await useMultiFileAuthState(authDir)
  const stateLid=identityAlias(state?.creds?.me?.lid)
  if (stateLid) {
    selfLid=stateLid
    setMeta.run('account_lid',stateLid)
    rememberIdentity([selfJid,selfLid],'')
  }
  const version = await fetchLiveWaWebVersion()
  setMeta.run('wa_web_version_source', proxyAgent ? 'live_tor_https' : 'live_direct_https')
  setMeta.run('baileys_version',version.join('.'))
  setMeta.run('transport',proxyUrl ? 'tor_socks' : 'direct')
  setMeta.run('transport_topology',proxyUrl ? 'shared_worker_tor' : 'direct')
  setMeta.run('qr_ready','false')
  setStatus('connecting')
  return await new Promise((resolve) => {
    let settled=false
    const sock=makeWASocket({
      version,auth:state,logger,printQRInTerminal:false,
      agent:proxyAgent,fetchAgent:proxyAgent,
      syncFullHistory:true,markOnlineOnConnect:false,
      browser:Browsers.ubuntu('Chrome'),
      shouldSyncHistoryMessage:()=>true,
      generateHighQualityLinkPreview:false
    })
    let activeBackfill=null
    let backfillTimeout=null
    let backfillPoll=null
    const finishBackfill=(status,extra={})=>{
      if (!activeBackfill) return
      clearTimeout(backfillTimeout)
      const after=historyStatsStmt.get(activeBackfill.jid)
      atomicJson(backfillResultPath,{
        request_id:activeBackfill.request_id,
        jid:activeBackfill.jid,
        count:activeBackfill.count,
        status,
        requested_at:activeBackfill.requested_at,
        finished_at:nowIso(),
        request_message_id:activeBackfill.request_message_id || null,
        before_count:activeBackfill.before_count,
        after_count:Number(after?.n || 0),
        added:Math.max(0,Number(after?.n || 0)-activeBackfill.before_count),
        before_oldest_date:activeBackfill.before_oldest_date || null,
        oldest_date:after?.oldest_date || null,
        ...extra
      })
      activeBackfill=null
      backfillTimeout=null
    }
    const pollBackfill=async()=>{
      if (activeBackfill) return
      const request=readBoundedJson(backfillRequestPath)
      if (!request) return
      try { fs.unlinkSync(backfillRequestPath) } catch {}
      const jid=safeJid(request.jid)
      const count=Number(request.count)
      if (!jid || jid!==request.jid || !Number.isInteger(count) || count<1 || count>50 || typeof request.request_id!=='string') {
        atomicJson(backfillResultPath,{request_id:request.request_id || null,jid:request.jid || null,status:'invalid_request',finished_at:nowIso()})
        return
      }
      const oldest=oldestMessageStmt.get(jid)
      if (!oldest) {
        atomicJson(backfillResultPath,{request_id:request.request_id,jid,status:'no_anchor_message',finished_at:nowIso()})
        return
      }
      const before=historyStatsStmt.get(jid)
      activeBackfill={
        request_id:request.request_id,jid,count,requested_at:nowIso(),
        before_count:Number(before?.n || 0),before_oldest_date:before?.oldest_date || null
      }
      atomicJson(backfillResultPath,{
        request_id:request.request_id,jid,count,status:'requested',requested_at:activeBackfill.requested_at,
        before_count:activeBackfill.before_count,before_oldest_date:activeBackfill.before_oldest_date
      })
      const key={remoteJid:jid,id:oldest.message_id,fromMe:Boolean(oldest.from_me)}
      if (jid.endsWith('@g.us') && oldest.sender_jid) key.participant=oldest.sender_jid
      const timestamp=Math.floor(new Date(oldest.date).getTime()/1000)
      try {
        activeBackfill.request_message_id=await sock.fetchMessageHistory(count,key,timestamp)
      } catch (err) {
        finishBackfill('request_error',{error:String(err?.message || err?.name || 'request_error').slice(0,200)})
        return
      }
      backfillTimeout=setTimeout(()=>finishBackfill('timeout_no_response'),45000)
    }
    backfillPoll=setInterval(()=>{ pollBackfill().catch(()=>{}) },1000)
    sock.ev.on('creds.update',saveCreds)
    sock.ev.on('lid-mapping.update',({lid,pn}={})=>{
      rememberIdentity([lid,pn],'')
    })
    sock.ev.on('contacts.upsert',(items)=>{ for(const x of items) rememberContact(x) })
    sock.ev.on('contacts.update',(items)=>{ for(const x of items) rememberContact(x) })
    sock.ev.on('chats.upsert',(items)=>{ for(const x of items) rememberChat(x) })
    sock.ev.on('chats.update',(items)=>{ for(const x of items) rememberChat(x) })
    sock.ev.on('messaging-history.set',({chats=[],contacts=[],messages=[],isLatest})=>{
      for(const x of contacts) rememberContact(x)
      for(const x of chats) rememberChat(x)
      storeBatch(messages,'history')
      if (isLatest) setMeta.run('history_synced_at',nowIso())
      setMeta.run('last_success_at',nowIso())
      if (activeBackfill) {
        const after=historyStatsStmt.get(activeBackfill.jid)
        const added=Math.max(0,Number(after?.n || 0)-activeBackfill.before_count)
        const older=Boolean(after?.oldest_date && (!activeBackfill.before_oldest_date || after.oldest_date<activeBackfill.before_oldest_date))
        const targetBatch=messages.some(item=>safeJid(item?.key?.remoteJid)===activeBackfill.jid)
        if (added>0 || older || targetBatch) {
          finishBackfill(added>0 || older ? 'received' : 'received_no_older_messages',{
            history_batch:messages.length
          })
        }
      }
      health({history_batch:messages.length})
    })
    sock.ev.on('messages.upsert',({messages=[]})=>{
      storeBatch(messages,'upsert')
      if (messages.length) {
        setMeta.run('last_success_at',nowIso())
        health()
      }
    })
    sock.ev.on('messages.update',(items)=>{
      for(const item of items || []) {
        if (item?.update?.message) {
          storeAnyMessage(
            {key:item.key,message:item.update.message,messageTimestamp:Date.now()/1000},
            'update'
          )
        }
      }
      health()
    })
    sock.ev.on('messages.reaction',(items)=>{
      for (const item of items || []) {
        if (item?.key && item?.reaction) {
          storeReaction(item.key,item.reaction,'live')
        }
      }
      if ((items || []).length) {
        setMeta.run('last_success_at',nowIso())
        health()
      }
    })
    sock.ev.on('messages.delete',(value)=>{
      const keys=Array.isArray(value?.keys) ? value.keys : []
      for(const key of keys) deleteMessage(key)
      health()
    })
    sock.ev.on('connection.update',async ({connection,lastDisconnect,qr})=>{
      if (qr) {
        await QRCode.toFile(qrPath,qr,{width:512,margin:2,errorCorrectionLevel:'M'})
        fs.chmodSync(qrPath,0o600)
        setMeta.run('qr_ready','true')
        setStatus('awaiting_qr')
        console.log('WHATSAPP_QR_READY')
      }
      if (connection==='open') {
        try { fs.unlinkSync(qrPath) } catch {}
        setMeta.run('qr_ready','false')
        const jid=safeJid(sock.user?.id)
        if (jid) {
          selfJid=jid
          setMeta.run('account_jid',jid)
          rememberIdentity([selfJid,selfLid],'')
        }
        if (!getMeta.get('linked_at')) setMeta.run('linked_at',nowIso())
        setMeta.run('last_success_at',nowIso())
        setStatus('connected')
        console.log('WHATSAPP_CONNECTED')
      }
      if (connection==='close' && !settled) {
        settled=true
        clearInterval(backfillPoll)
        if (activeBackfill) finishBackfill('connection_closed')
        else clearTimeout(backfillTimeout)
        const statusCode=lastDisconnect?.error?.output?.statusCode || lastDisconnect?.error?.statusCode
        const reason=lastDisconnect?.error?.message || lastDisconnect?.error?.name || 'unknown'
        setMeta.run('last_disconnect_code',String(statusCode || 'unknown'))
        setMeta.run('last_disconnect_reason',String(reason).slice(0,200))
        const loggedOut=statusCode===DisconnectReason.loggedOut
        setMeta.run('qr_ready','false')
        setStatus(loggedOut ? 'logged_out' : 'disconnected')
        resolve(loggedOut ? 'logged_out' : 'retry')
      }
    })
  })
}
const sleep=(ms)=>new Promise(resolve=>setTimeout(resolve,ms))
async function main() {
  health()
  for (;;) {
    try {
      const result=await connectOnce()
      if (result==='logged_out') {
        console.error('WHATSAPP_LOGGED_OUT')
        await sleep(30000)
      } else {
        await sleep(3000)
      }
    } catch (err) {
      setStatus('error',{last_error:err?.name || 'Error'})
      console.error('WHATSAPP_ERROR',err?.name || 'Error')
      await sleep(5000)
    }
  }
}
process.on('SIGTERM',()=>{ health({stopping:true}); db.close(); process.exit(0) })
process.on('SIGINT',()=>{ health({stopping:true}); db.close(); process.exit(0) })
await main()
