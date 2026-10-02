"""Hosted public dependency candidates only; never application build or signing."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile


def run(*argv, capture=False):
    return subprocess.check_output(argv, text=True).strip() if capture else subprocess.run(argv, check=True)


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('component', choices=['api', 'calendar', 'worker', 'whatsapp'])
    args = parser.parse_args()
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        raise SystemExit('hosted candidate capture required')
    root = Path.cwd()
    inputs = json.loads((root / 'inputs.json').read_text())
    if not re.fullmatch('[a-f0-9]{40}', inputs.get('canonical_source', '')):
        raise SystemExit('reviewed exact dependency source required')
    for name, expected in inputs['files'].items():
        if digest(root / name) != expected:
            raise SystemExit('dependency input identity mismatch')
    component = args.component
    output = root / 'evidence'
    output.mkdir(exist_ok=False)
    # Worker uses one supported distribution throughout: no Ubuntu binaries in
    # Debian, and no interpreter or shared objects copied across distributions.
    parent = ('node:22-trixie-slim' if component == 'whatsapp' else
              'ubuntu:24.04' if component == 'worker' else 'python:3.12-slim-trixie')
    run('docker', 'pull', '--platform', 'linux/amd64', parent)
    inspected = json.loads(run('docker', 'image', 'inspect', parent, capture=True))[0]
    index_ref = inspected['RepoDigests'][0]
    index = json.loads(run('docker', 'buildx', 'imagetools', 'inspect', index_ref, '--raw', capture=True))
    parent_ref = index_ref
    if 'manifests' in index:
        selected = [m for m in index['manifests'] if m.get('platform', {}).get('os') == 'linux'
                    and m.get('platform', {}).get('architecture') == 'amd64'
                    and not m.get('platform', {}).get('variant')]
        if len(selected) != 1:
            raise SystemExit('unique Linux amd64 parent manifest required')
        parent_ref = index_ref.split('@')[0] + '@' + selected[0]['digest']
        run('docker', 'pull', '--platform', 'linux/amd64', parent_ref)
        exact = json.loads(run('docker', 'image', 'inspect', parent_ref, capture=True))[0]
        if exact['Id'] != inspected['Id']:
            raise SystemExit('platform manifest config mismatch')
        inspected = exact
    if '@sha256:' not in parent_ref or inspected['Os'] != 'linux' or inspected['Architecture'] != 'amd64':
        raise SystemExit('immutable parent identity required')
    (output / 'parent-inspect.json').write_text(json.dumps(inspected, indent=2) + '\n')
    (output / 'parent-index.json').write_text(json.dumps(index, indent=2) + '\n')
    lines = ['FROM ' + parent_ref + ' AS deps-build', 'WORKDIR /app']
    if component == 'worker':
        lines += ['RUN apt-get update && apt-get install -y --no-install-recommends python3.12-venv ca-certificates && rm -rf /var/lib/apt/lists/*',
                  'RUN python3.12 -m venv /opt/tm-python',
                  'ENV PATH=/opt/tm-python/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin']
        native_source = 'db20f322d03664d1e878e2fbf6e904f5da755594'
        gtest_source = '7d76a231b0e29caf86e68d1df858308cd53b2a66'
        native_tests = ['normproto_test', 'fullyconnected_test', 'genericvector_test',
                        'unicharset_load_test', 'intproto_test', 'plumbing_test',
                        'recoder_test', 'lstm_layer_test']
        targets = ' '.join(native_tests)
        test_pattern = '^(' + '|'.join(native_tests) + ')$'
        common = '-DBUILD_SHARED_LIBS=ON -DBUILD_TESTS=ON -DBUILD_TRAINING_TOOLS=ON -DDISABLE_ARCHIVE=ON -DDISABLE_CURL=ON -DGRAPHICS_DISABLED=ON -DENABLE_NATIVE=OFF -DCMAKE_INSTALL_LIBDIR=lib'
        lines += ['RUN apt-get update && apt-get install -y --no-install-recommends git build-essential cmake pkg-config libleptonica-dev libicu-dev && rm -rf /var/lib/apt/lists/*',
                  'RUN git init /tesseract-source && git -C /tesseract-source remote add origin https://github.com/tesseract-ocr/tesseract.git && git -C /tesseract-source fetch --depth=1 origin ' + native_source + ' && git -C /tesseract-source checkout --detach FETCH_HEAD && test "$(git -C /tesseract-source rev-parse HEAD)" = ' + native_source,
                  'RUN git init /tesseract-source/unittest/third_party/googletest && git -C /tesseract-source/unittest/third_party/googletest remote add origin https://github.com/google/googletest.git && git -C /tesseract-source/unittest/third_party/googletest fetch --depth=1 origin ' + gtest_source + ' && git -C /tesseract-source/unittest/third_party/googletest checkout --detach FETCH_HEAD && test "$(git -C /tesseract-source/unittest/third_party/googletest rev-parse HEAD)" = ' + gtest_source,
                  'RUN cmake -S /tesseract-source -B /tesseract-release -DCMAKE_BUILD_TYPE=Release ' + common + ' && cmake --build /tesseract-release --parallel 2 --target tesseract ' + targets,
                  'RUN cd /tesseract-release && ctest --no-tests=error --output-on-failure --output-junit /native-release-tests.xml -R "' + test_pattern + '"',
                  'RUN cmake -S /tesseract-source -B /tesseract-sanitized -DCMAKE_BUILD_TYPE=Debug ' + common + ' -DCMAKE_C_FLAGS="-fsanitize=address,undefined -fno-omit-frame-pointer" -DCMAKE_CXX_FLAGS="-fsanitize=address,undefined -fno-omit-frame-pointer" -DCMAKE_EXE_LINKER_FLAGS="-fsanitize=address,undefined" -DCMAKE_SHARED_LINKER_FLAGS="-fsanitize=address,undefined" && cmake --build /tesseract-sanitized --parallel 2 --target ' + targets,
                  'RUN cd /tesseract-sanitized && ASAN_OPTIONS=detect_leaks=0:halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1 ctest --no-tests=error --output-on-failure --output-junit /native-sanitized-tests.xml -R "' + test_pattern + '"',
                  'RUN mkdir -p /tesseract-runtime/bin /tesseract-runtime/lib /tesseract-runtime/licenses && cp /tesseract-release/bin/tesseract /tesseract-runtime/bin/ && cp -a /tesseract-release/libtesseract.so* /tesseract-runtime/lib/ && cp /tesseract-source/LICENSE /tesseract-runtime/licenses/LICENSE && git -C /tesseract-source archive HEAD > /native-source.tar && git -C /tesseract-source/unittest/third_party/googletest archive HEAD > /native-test-source.tar']
    if component == 'whatsapp':
        lines += ['RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git python3 make g++ && rm -rf /var/lib/apt/lists/*',
                  'COPY whatsapp/package.json whatsapp/package-lock.json /app/whatsapp/',
                  'COPY whatsapp/patches /app/whatsapp/patches',
                  'WORKDIR /app/whatsapp',
                  'RUN git config --global url."https://github.com/".insteadOf ssh://git@github.com/ && git config --global --add url."https://github.com/".insteadOf git@github.com: && npm ci --include=dev && npm prune --omit=dev && npm cache clean --force']
    else:
        reqs = {'api': ['requirements.lock', 'requirements.v24.txt'], 'calendar': ['requirements.calendar.txt'], 'worker': ['requirements.worker.txt']}[component]
        pip_args = ' '.join('-r /dependency-inputs/' + p for p in reqs)
        lines += ['COPY ' + ' '.join(reqs) + ' /dependency-inputs/',
                  'RUN mkdir /dependency-artifacts /dependency-wheels && python -m pip download --only-binary=:all: --no-binary=pyaes --dest /dependency-artifacts setuptools==80.9.0 wheel==0.46.2 ' + pip_args,
                  'RUN python -m pip install --no-index --find-links=/dependency-artifacts setuptools==80.9.0 wheel==0.46.2 && python -m pip wheel --no-index --no-build-isolation --find-links=/dependency-artifacts --wheel-dir=/dependency-wheels ' + pip_args,
                  'RUN python -m pip install --no-index --find-links=/dependency-wheels ' + pip_args + ' && python -m pip check && python -m pip uninstall -y setuptools wheel']
    lines += ['FROM ' + parent_ref,
              'ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1',
              'RUN groupadd --gid 10001 tmdeps && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin tmdeps',
              'RUN mkdir -p /dependency-debs/partial && apt-get update && apt-get -o Dir::Cache::archives=/dependency-debs/ -o APT::Keep-Downloaded-Packages=true upgrade -y && rm -rf /var/lib/apt/lists/*',
              'WORKDIR /app']
    if component == 'whatsapp':
        lines += ['ENV NODE_ENV=production',
                  'RUN apt-get update && apt-get -o Dir::Cache::archives=/dependency-debs/ -o APT::Keep-Downloaded-Packages=true install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*',
                  'COPY --from=deps-build /app/whatsapp/node_modules /app/whatsapp/node_modules',
                  'COPY whatsapp/package.json whatsapp/package-lock.json /app/whatsapp/',
                  'RUN rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack /opt/yarn-v* && rm -f /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/yarn /usr/local/bin/yarnpkg /usr/local/bin/corepack']
    else:
        if component == 'worker':
            lines += ['RUN apt-get update && apt-get -o Dir::Cache::archives=/dependency-debs/ -o APT::Keep-Downloaded-Packages=true install -y --no-install-recommends python3.12 liblept5 libstdc++6 libxml2 tesseract-ocr-rus tesseract-ocr-eng libgomp1 ca-certificates tor fonts-dejavu-core && dpkg --compare-versions "$(dpkg-query -W -f=\'${Version}\' libxml2)" ge 2.9.14+dfsg-1.3ubuntu3.9 && rm -rf /var/lib/apt/lists/*',
                      'COPY --from=deps-build /opt/tm-python /opt/tm-python',
                      'COPY --from=deps-build /tesseract-runtime/bin/tesseract /usr/local/bin/tesseract',
                      'COPY --from=deps-build /tesseract-runtime/lib/ /usr/local/lib/',
                      'COPY --from=deps-build /tesseract-runtime/licenses/ /usr/local/share/licenses/tesseract/',
                      'RUN ldconfig',
                      'RUN /opt/tm-python/bin/python -m pip uninstall -y pip && ln -s /opt/tm-python/bin/python /usr/local/bin/python',
                      'ENV PATH=/opt/tm-python/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
                      'ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata',
                      'ENV TM_SERVER_WORKER=1 TM_WORKER_STATE_DIR=/state TM_TELEGRAM_SESSION=/state/telegram_helper.session TM_MODELS_HOME=/models-root TM_MODELS_DIR=/models TM_AUDIO_PYTHON=/usr/local/bin/python TM_MEDIA_BACKEND=linux TM_OCR_LANG=rus+eng']
        else:
            lines += ['COPY --from=deps-build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages',
                      'COPY --from=deps-build /usr/local/bin /usr/local/bin']
    lines += ['USER 10001:10001', 'ENTRYPOINT []', 'CMD ["/bin/false"]']
    recipe = '\n'.join(lines) + '\n'
    (root / 'Dependency.Dockerfile').write_text(recipe)
    (output / 'Dependency.Dockerfile').write_text(recipe)
    tag = 'tm-dependency-candidate:' + component
    builder = 'tm-dependency-builder:' + component
    run('docker', 'build', '--platform', 'linux/amd64', '--target', 'deps-build', '-f', str(root / 'Dependency.Dockerfile'), '-t', builder, str(root))
    run('docker', 'build', '--platform', 'linux/amd64', '-f', str(root / 'Dependency.Dockerfile'), '-t', tag, str(root))
    if component == 'worker':
        native_evidence = '''import pathlib,json,hashlib,subprocess,xml.etree.ElementTree as ET
def h(path): return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()
reports=[]
for path in ['/native-release-tests.xml','/native-sanitized-tests.xml']:
 root=ET.parse(path).getroot();cases=root.findall('.//testcase')
 assert len(cases)==8 and not root.findall('.//failure') and not root.findall('.//error') and not root.findall('.//skipped')
 reports.append({'path':path,'sha256':h(path),'test_suites':[x.attrib for x in cases],'xml':pathlib.Path(path).read_text()})
print(json.dumps({'upstream_commit':subprocess.check_output(['git','-C','/tesseract-source','rev-parse','HEAD'],text=True).strip(),'upstream_tree':subprocess.check_output(['git','-C','/tesseract-source','rev-parse','HEAD^{tree}'],text=True).strip(),'googletest_commit':subprocess.check_output(['git','-C','/tesseract-source/unittest/third_party/googletest','rev-parse','HEAD'],text=True).strip(),'source_archive_sha256':h('/native-source.tar'),'test_source_archive_sha256':h('/native-test-source.tar'),'runtime_binary_sha256':h('/tesseract-runtime/bin/tesseract'),'upstream_license_sha256':h('/tesseract-source/LICENSE'),'upstream_license_text':pathlib.Path('/tesseract-source/LICENSE').read_text(),'reports':reports,'asan_ubsan_halt_on_error':True,'asan_leak_detection':False,'original_application_started':False,'independently_accepted':False}))'''
        native_data = json.loads(run('docker', 'run', '--rm', '--network', 'none', '--read-only',
                                    '--entrypoint', 'python', builder, '-c', native_evidence, capture=True))
        if native_data['upstream_commit'] != native_source or native_data['googletest_commit'] != gtest_source:
            raise SystemExit('native source identity mismatch')
        (output / 'native-build-evidence.json').write_text(json.dumps(native_data, indent=2) + '\n')
    image = json.loads(run('docker', 'image', 'inspect', tag, capture=True))[0]
    (output / 'candidate-inspect.json').write_text(json.dumps(image, indent=2) + '\n')
    # Actual files and resolved package-documentation links, not SPDX guesses.
    notice_cmd = '''import pathlib,json,hashlib,subprocess
packages=subprocess.check_output(['dpkg-query','-W','-f=${binary:Package}\\t${Version}\\n'],text=True).splitlines()
records=[]
for row in packages:
 package,version=row.split('\\t');path=pathlib.Path('/usr/share/doc')/package.split(':')[0]/'copyright'
 item={'package':package,'version':version,'requested_path':str(path)}
 if path.exists():
  resolved=path.resolve(strict=True)
  if not resolved.is_relative_to('/usr/share/doc'): raise SystemExit('copyright path escaped documentation root')
  content=resolved.read_bytes()
  if len(content)>1048576: raise SystemExit('notice exceeds bound')
  item.update(resolved_path=str(resolved),sha256=hashlib.sha256(content).hexdigest(),text=content.decode('utf-8',errors='replace'))
 else: item['missing']=True
 records.append(item)
links=[]
root=pathlib.Path('/app')
for path in root.rglob('*'):
 if path.is_symlink():
  resolved=path.resolve(strict=True)
  if not resolved.is_relative_to(root) or not resolved.is_file(): raise SystemExit('application dependency link escapes or dangles')
  links.append({'path':str(path),'target':str(path.readlink()),'resolved':str(resolved),'sha256':hashlib.sha256(resolved.read_bytes()).hexdigest()})
print(json.dumps({'package_notices':records,'app_links':links,'independently_accepted':False}))'''
    if component != 'whatsapp':
        (output / 'actual-package-notices-and-links.json').write_text(run(
            'docker', 'run', '--rm', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges', '--entrypoint', 'python', tag, '-c', notice_cmd, capture=True) + '\n')
    if component == 'whatsapp':
        smoke = "const D=require('/app/whatsapp/node_modules/better-sqlite3');const d=new D(':memory:');d.exec('create table replay(id text primary key)');d.prepare('insert or ignore into replay values (?)').run('synthetic');d.prepare('insert or ignore into replay values (?)').run('synthetic');if(d.prepare('select count(*) n from replay').get().n!==1)throw Error('sqlite dedupe');d.close();import('/app/whatsapp/node_modules/@whiskeysockets/baileys/lib/index.js').then(()=>console.log(JSON.stringify({sqlite_native_abi:true,baileys_import:true,application_started:false})))"
        (output / 'dependency-smoke.json').write_text(run('docker', 'run', '--rm', '--network', 'none', '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--entrypoint', 'node', tag, '-e', smoke, capture=True) + '\n')
    else:
        imports = {'api': 'import mcp,uvicorn,psycopg,cryptography,icalendar',
                   'calendar': 'import radicale,gunicorn,psycopg,argon2,icalendar',
                   'worker': 'import telethon,postgrest,av,numpy,faster_whisper,pymupdf,pytesseract;from PIL import Image;import io;image=Image.new("RGB",(16,16),(255,255,255));buffer=io.BytesIO();image.save(buffer,format="PNG");buffer.seek(0);decoded=Image.open(buffer);decoded.load();assert decoded.size==(16,16)'}[component]
        (output / 'dependency-smoke.json').write_text(run('docker', 'run', '--rm', '--network', 'none', '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--entrypoint', 'python', tag, '-c', imports + ';import json;print(json.dumps({"imports_verified":True,"application_started":False}))', capture=True) + '\n')
        if component == 'worker':
            # Actual native OCR, PDF, codec and XML ABI checks, with synthetic
            # payloads only. No Telegram session, model download or application.
            native_smoke = '''import io,json,ctypes,ctypes.util,wave,av,pymupdf,pytesseract
from PIL import Image,ImageDraw,ImageFont
image=Image.new('RGB',(650,100),'white');draw=ImageDraw.Draw(image)
draw.text((15,15),'SYNTHETIC 123',font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',36),fill='black')
text=pytesseract.image_to_string(image,config='--psm 7',lang='eng').strip()
assert 'SYNTHETIC' in text and '123' in text,text
data=pytesseract.image_to_data(image,config='--psm 7',lang='eng',output_type=pytesseract.Output.DICT)
assert 'SYNTHETIC' in data['text'] and '123' in data['text'],data['text']
assert {'eng','rus'}<=set(pytesseract.get_languages(config=''))
doc=pymupdf.open();page=doc.new_page();page.insert_text((30,40),'SYNTHETIC 123');pdf=doc.tobytes();doc.close()
with pymupdf.open(stream=pdf,filetype='pdf') as doc:
 pix=doc[0].get_pixmap();assert pix.width>0 and pix.height>0
audio=io.BytesIO()
with wave.open(audio,'wb') as wav:
 wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000);wav.writeframes(b'\\0'*32000)
audio.seek(0)
with av.open(audio,format='wav') as container: assert sum(f.samples for f in container.decode(audio=0))==16000
xml=ctypes.CDLL(ctypes.util.find_library('xml2'));xml.xmlReadMemory.argtypes=[ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_char_p,ctypes.c_int];xml.xmlReadMemory.restype=ctypes.c_void_p;xml.xmlFreeDoc.argtypes=[ctypes.c_void_p]
payload=b'<synthetic><value>123</value></synthetic>';ptr=xml.xmlReadMemory(payload,len(payload),None,None,2048);assert ptr;xml.xmlFreeDoc(ptr)
print(json.dumps({'ocr_text':text,'ocr_word_data':True,'russian_english_models_present':True,'pdf_native_render':True,'wav_native_decode':True,'xml_abi_smoke':True,'security_regression_poc':False,'application_started':False}))'''
            (output / 'worker-native-smoke.json').write_text(run(
                'docker', 'run', '--rm', '--network', 'none', '--read-only', '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m',
                '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--entrypoint', 'python', tag, '-c', native_smoke, capture=True) + '\n')
    (output / 'apt-inventory.txt').write_text(run('docker', 'run', '--rm', '--network', 'none', '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--entrypoint', 'dpkg-query', tag, '-W', '-f=${Package}\t${Version}\t${Architecture}\n', capture=True) + '\n')
    deb_cmd = "import pathlib,hashlib,json;print(json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in pathlib.Path('/dependency-debs').glob('*.deb')}))"
    if component == 'worker':
        (output / 'deb-hashes.json').write_text(run('docker', 'run', '--rm', '--network', 'none', '--read-only', '--entrypoint', 'python', tag, '-c', deb_cmd, capture=True) + '\n')
    if component != 'whatsapp':
        freeze_cmd = "import importlib.metadata as m;print('\\n'.join(sorted(d.metadata['Name']+'=='+d.version for d in m.distributions())))"
        (output / 'python-freeze.txt').write_text(run('docker', 'run', '--rm', '--network', 'none', '--read-only', '--entrypoint', 'python', tag, '-c', freeze_cmd, capture=True) + '\n')
        inventory = "import importlib.metadata as m,json;print(json.dumps([{'name':d.metadata['Name'],'version':d.version,'license':d.metadata.get('License'),'license_expression':d.metadata.get('License-Expression')} for d in m.distributions()],sort_keys=True))"
        (output / 'python-licenses.json').write_text(run('docker', 'run', '--rm', '--network', 'none', '--read-only', '--entrypoint', 'python', tag, '-c', inventory, capture=True) + '\n')
        wheel_cmd = "import pathlib,hashlib,json;print(json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in pathlib.Path('/dependency-wheels').glob('*')}))"
        artifact_cmd = wheel_cmd.replace('/dependency-wheels', '/dependency-artifacts')
        (output / 'original-package-hashes.json').write_text(run('docker', 'run', '--rm', '--network', 'none', '--read-only', '--entrypoint', 'python', builder, '-c', artifact_cmd, capture=True) + '\n')
        (output / 'wheel-hashes.json').write_text(run('docker', 'run', '--rm', '--network', 'none', '--read-only', '--entrypoint', 'python', builder, '-c', wheel_cmd, capture=True) + '\n')
        lock_cmd = "import pathlib,zipfile,email.parser,hashlib;items=[];\nfor p in pathlib.Path('/dependency-wheels').glob('*.whl'):\n z=zipfile.ZipFile(p);names=[n for n in z.namelist() if n.endswith('.dist-info/METADATA')];assert len(names)==1;m=email.parser.BytesParser().parsebytes(z.read(names[0]));items.append(m['Name']+'=='+m['Version']+' --hash=sha256:'+hashlib.sha256(p.read_bytes()).hexdigest())\nprint('\\n'.join(sorted(items)))"
        (output / 'resolved-requirements.lock').write_text(run('docker', 'run', '--rm', '--network', 'none', '--read-only', '--entrypoint', 'python', builder, '-c', lock_cmd, capture=True) + '\n')
    else:
        notice_js = '''const fs=require('fs'),p=require('path'),cp=require('child_process'),crypto=require('crypto');
const hash=b=>crypto.createHash('sha256').update(b).digest('hex');
const within=(r,t)=>t===r||t.startsWith(r+'/');
let records=[],links=[],npm=[];
for(const row of cp.execFileSync('dpkg-query',['-W','-f=${binary:Package}\\t${Version}\\n'],{encoding:'utf8'}).trim().split('\\n')){
 const [name,version]=row.split('\\t'),path='/usr/share/doc/'+name.split(':')[0]+'/copyright';
 const item={package:name,version,requested_path:path};
 if(fs.existsSync(path)){const resolved=fs.realpathSync(path);if(!within('/usr/share/doc',resolved))throw Error('notice path escaped');const b=fs.readFileSync(resolved);if(b.length>1048576)throw Error('notice bound');Object.assign(item,{resolved_path:resolved,sha256:hash(b),text:b.toString('utf8')});}else item.missing=true;
 records.push(item);
}
function walk(root){for(const x of fs.readdirSync(root,{withFileTypes:true})){
 const path=p.join(root,x.name);
 if(x.isSymbolicLink()){const resolved=fs.realpathSync(path);if(!within('/app',resolved)||!fs.statSync(resolved).isFile())throw Error('app link escaped or dangling');links.push({path,target:fs.readlinkSync(path),resolved,sha256:hash(fs.readFileSync(resolved))});}
 else if(x.isDirectory())walk(path);
 else if(x.isFile()&&/^(license|license.txt|license.md|copying)$/i.test(x.name)){const b=fs.readFileSync(path);if(b.length>1048576)throw Error('notice bound');npm.push({path,sha256:hash(b),text:b.toString('utf8')});}
}}walk('/app');
console.log(JSON.stringify({package_notices:records,npm_notice_files:npm,app_links:links,independently_accepted:false}));'''
        (output / 'actual-package-notices-and-links.json').write_text(run(
            'docker', 'run', '--rm', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges', '--entrypoint', 'node', tag, '-e', notice_js, capture=True) + '\n')
        node_cmd = "const fs=require('fs'),path=require('path');let a=[];function walk(p){for(const x of fs.readdirSync(p,{withFileTypes:true})){if(x.isDirectory()&&!x.isSymbolicLink())walk(path.join(p,x.name));else if(x.name==='package.json'){try{let j=JSON.parse(fs.readFileSync(path.join(p,x.name)));a.push({path:path.relative('/app/whatsapp',p),name:j.name,version:j.version,license:j.license||null})}catch(e){throw e}}}}walk('/app/whatsapp/node_modules');console.log(JSON.stringify(a))"
        (output / 'npm-licenses.json').write_text(run('docker', 'run', '--rm', '--network', 'none', '--read-only', '--entrypoint', 'node', tag, '-e', node_cmd, capture=True) + '\n')
    archive = output / 'candidate-image.tar'
    run('docker', 'save', '--output', str(archive), tag)
    if archive.stat().st_size > 2 * 1024**3:
        raise SystemExit('candidate archive size limit')
    members = {}
    with tarfile.open(archive, 'r:') as stream:
        for item in stream:
            if item.isdir():
                continue
            if not item.isfile() or item.name in members or item.name.startswith('/') or '..' in Path(item.name).parts:
                raise SystemExit('candidate archive unsafe member')
            h = hashlib.sha256()
            with stream.extractfile(item) as source:
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    h.update(block)
            members[item.name] = {'sha256': h.hexdigest(), 'bytes': item.size}
        manifest = json.load(stream.extractfile('manifest.json'))
        if len(manifest) != 1:
            raise SystemExit('one saved image required')
        saved = manifest[0]
        if 'sha256:' + members[saved['Config']]['sha256'] != image['Id']:
            raise SystemExit('saved config image identity mismatch')
        if ['sha256:' + members[p]['sha256'] for p in saved['Layers']] != image['RootFS']['Layers']:
            raise SystemExit('saved layer identity mismatch')
    (output / 'saved-member-hashes.json').write_text(json.dumps(members, indent=2) + '\n')
    evidence = {'contract_version': 'tm-dependency-candidate/v1', 'component': component,
                'canonical_source': inputs['canonical_source'], 'public_commit': os.environ['GITHUB_SHA'],
                'run_id': os.environ['GITHUB_RUN_ID'], 'run_attempt': os.environ['GITHUB_RUN_ATTEMPT'],
                'parent_ref': parent_ref, 'parent_image_id': inspected['Id'], 'candidate_image_id': image['Id'],
                'parent_index_ref': index_ref,
                'candidate_rootfs_diff_ids': image['RootFS']['Layers'],
                'archive_sha256': digest(archive), 'archive_bytes': archive.stat().st_size,
                'recipe_sha256': hashlib.sha256(recipe.encode()).hexdigest(),
                'registry_manifest_ref': None, 'provenance_verified': False, 'license_reviewed': False,
                'vulnerability_reviewed': False, 'legacy_V22_environment_match': False,
                'original_application_built': False, 'production_ready': False, 'release_authorized': False}
    (output / 'candidate.json').write_text(json.dumps(evidence, indent=2) + '\n')


if __name__ == '__main__':
    main()
