"""Archive only manifest-listed files; verify hashes and reject path traversal."""
import hashlib,json,pathlib,sys,zipfile
root=pathlib.Path(sys.argv[1]).resolve()
output=pathlib.Path(sys.argv[2]).resolve()
manifest=json.loads((root/'source-manifest.json').read_text(encoding='utf-8'))
if output.exists():raise SystemExit('Archive already exists; choose a new output')
payloads=[]
for entry in manifest['files']:
    rel=pathlib.PurePosixPath(entry['path'])
    if rel.is_absolute() or '..' in rel.parts:raise SystemExit('Unsafe manifest path')
    item=(root/str(rel)).resolve()
    if not item.is_relative_to(root):raise SystemExit('Manifest path escapes root')
    data=item.read_bytes()
    if hashlib.sha256(data).hexdigest()!=entry['sha256']:raise SystemExit('Hash mismatch: '+str(rel))
    payloads.append((str(rel),data))
payloads.append(('source-manifest.json',(root/'source-manifest.json').read_bytes()))
with zipfile.ZipFile(output,'x',compression=zipfile.ZIP_DEFLATED) as archive:
    for name,data in payloads:archive.writestr(root.name+'/'+name,data)
digest=hashlib.sha256(output.read_bytes()).hexdigest()
output.with_suffix(output.suffix+'.sha256').write_text(digest+'  '+output.name+'\n',encoding='ascii')
print(json.dumps({'archive':str(output),'sha256':digest,'files':len(payloads)}))
