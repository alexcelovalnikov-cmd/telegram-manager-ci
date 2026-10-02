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
    parent = 'node:22-trixie-slim' if component == 'whatsapp' else 'python:3.12-slim-trixie'
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
        lines += ['COPY --from=deps-build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages',
                  'COPY --from=deps-build /usr/local/bin /usr/local/bin']
        if component == 'worker':
            lines += ['RUN apt-get update && apt-get -o Dir::Cache::archives=/dependency-debs/ -o APT::Keep-Downloaded-Packages=true install -y --no-install-recommends tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng libgomp1 ca-certificates tor && rm -rf /var/lib/apt/lists/*',
                      'ENV TM_SERVER_WORKER=1 TM_WORKER_STATE_DIR=/state TM_TELEGRAM_SESSION=/state/telegram_helper.session TM_MODELS_HOME=/models-root TM_MODELS_DIR=/models TM_AUDIO_PYTHON=/usr/local/bin/python TM_MEDIA_BACKEND=linux TM_OCR_LANG=rus+eng']
    lines += ['USER 10001:10001', 'ENTRYPOINT []', 'CMD ["/bin/false"]']
    recipe = '\n'.join(lines) + '\n'
    (root / 'Dependency.Dockerfile').write_text(recipe)
    (output / 'Dependency.Dockerfile').write_text(recipe)
    tag = 'tm-dependency-candidate:' + component
    builder = 'tm-dependency-builder:' + component
    run('docker', 'build', '--platform', 'linux/amd64', '--target', 'deps-build', '-f', str(root / 'Dependency.Dockerfile'), '-t', builder, str(root))
    run('docker', 'build', '--platform', 'linux/amd64', '-f', str(root / 'Dependency.Dockerfile'), '-t', tag, str(root))
    image = json.loads(run('docker', 'image', 'inspect', tag, capture=True))[0]
    (output / 'candidate-inspect.json').write_text(json.dumps(image, indent=2) + '\n')
    if component == 'whatsapp':
        smoke = "const D=require('/app/whatsapp/node_modules/better-sqlite3');const d=new D(':memory:');d.exec('create table replay(id text primary key)');d.prepare('insert or ignore into replay values (?)').run('synthetic');d.prepare('insert or ignore into replay values (?)').run('synthetic');if(d.prepare('select count(*) n from replay').get().n!==1)throw Error('sqlite dedupe');d.close();import('/app/whatsapp/node_modules/@whiskeysockets/baileys/lib/index.js').then(()=>console.log(JSON.stringify({sqlite_native_abi:true,baileys_import:true,application_started:false})))"
        (output / 'dependency-smoke.json').write_text(run('docker', 'run', '--rm', '--network', 'none', '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--entrypoint', 'node', tag, '-e', smoke, capture=True) + '\n')
    else:
        imports = {'api': 'import mcp,uvicorn,psycopg,cryptography,icalendar',
                   'calendar': 'import radicale,gunicorn,psycopg,argon2,icalendar',
                   'worker': 'import telethon,postgrest,av,numpy,faster_whisper,pymupdf,pytesseract;from PIL import Image;import io;image=Image.new("RGB",(16,16),(255,255,255));buffer=io.BytesIO();image.save(buffer,format="PNG");buffer.seek(0);decoded=Image.open(buffer);decoded.load();assert decoded.size==(16,16)'}[component]
        (output / 'dependency-smoke.json').write_text(run('docker', 'run', '--rm', '--network', 'none', '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--entrypoint', 'python', tag, '-c', imports + ';import json;print(json.dumps({"imports_verified":True,"application_started":False}))', capture=True) + '\n')
    (output / 'apt-inventory.txt').write_text(run('docker', 'run', '--rm', '--network', 'none', '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--entrypoint', 'dpkg-query', tag, '-W', '-f=${Package}\t${Version}\t${Architecture}\n', capture=True) + '\n')
    deb_cmd = "import pathlib,hashlib,json;print(json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in pathlib.Path('/dependency-debs').glob('*.deb')}))"
    if component == 'worker':
        (output / 'deb-hashes.json').write_text(run('docker', 'run', '--rm', '--network', 'none', '--read-only', '--entrypoint', 'python', tag, '-c', deb_cmd, capture=True) + '\n')
    if component != 'whatsapp':
        (output / 'python-freeze.txt').write_text(run('docker', 'run', '--rm', '--network', 'none', '--read-only', '--entrypoint', 'python', tag, '-m', 'pip', 'freeze', '--all', capture=True) + '\n')
        inventory = "import importlib.metadata as m,json;print(json.dumps([{'name':d.metadata['Name'],'version':d.version,'license':d.metadata.get('License'),'license_expression':d.metadata.get('License-Expression')} for d in m.distributions()],sort_keys=True))"
        (output / 'python-licenses.json').write_text(run('docker', 'run', '--rm', '--network', 'none', '--read-only', '--entrypoint', 'python', tag, '-c', inventory, capture=True) + '\n')
        wheel_cmd = "import pathlib,hashlib,json;print(json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in pathlib.Path('/dependency-wheels').glob('*')}))"
        artifact_cmd = wheel_cmd.replace('/dependency-wheels', '/dependency-artifacts')
        (output / 'original-package-hashes.json').write_text(run('docker', 'run', '--rm', '--network', 'none', '--read-only', '--entrypoint', 'python', builder, '-c', artifact_cmd, capture=True) + '\n')
        (output / 'wheel-hashes.json').write_text(run('docker', 'run', '--rm', '--network', 'none', '--read-only', '--entrypoint', 'python', builder, '-c', wheel_cmd, capture=True) + '\n')
        lock_cmd = "import pathlib,zipfile,email.parser,hashlib;items=[];\nfor p in pathlib.Path('/dependency-wheels').glob('*.whl'):\n z=zipfile.ZipFile(p);names=[n for n in z.namelist() if n.endswith('.dist-info/METADATA')];assert len(names)==1;m=email.parser.BytesParser().parsebytes(z.read(names[0]));items.append(m['Name']+'=='+m['Version']+' --hash=sha256:'+hashlib.sha256(p.read_bytes()).hexdigest())\nprint('\\n'.join(sorted(items)))"
        (output / 'resolved-requirements.lock').write_text(run('docker', 'run', '--rm', '--network', 'none', '--read-only', '--entrypoint', 'python', builder, '-c', lock_cmd, capture=True) + '\n')
    else:
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
