"""Verify and extract the exact preview.3 candidate ZIP, including on Windows CI."""
import argparse,hashlib,json
from pathlib import Path,PurePosixPath
import sys,zipfile

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--release-dir',type=Path,required=True);p.add_argument('--extract',type=Path,required=True);args=p.parse_args()
    root=args.release_dir.resolve();destination=args.extract.resolve()
    expected={'Project-Relay-Local-v0.2.0-preview.3.zip','Project-Relay-Plugin-Template-v0.2.0-preview.3.zip','README.md','INSTALL.md','RELEASE_NOTES.md','LICENSE'}
    sums={}
    for line in (root/'SHA256SUMS.txt').read_text().splitlines():
        digest,name=line.split('  ',1)
        if name in sums or name not in expected:raise ValueError('Invalid checksum inventory')
        sums[name]=digest
    if set(sums)!=expected:raise ValueError('Incomplete checksum inventory')
    for name,digest in sums.items():
        if hashlib.sha256((root/name).read_bytes()).hexdigest()!=digest:raise ValueError('SHA mismatch: '+name)
    if destination.exists():raise ValueError('Extraction destination must be new')
    with zipfile.ZipFile(root/'Project-Relay-Local-v0.2.0-preview.3.zip') as z:
        names=z.namelist()
        if len(set(names))!=len(names) or sum(i.file_size for i in z.infolist())>64*1024*1024:raise ValueError('Invalid ZIP inventory')
        for info in z.infolist():
            rel=PurePosixPath(info.filename)
            if rel.is_absolute() or '..' in rel.parts or '\\' in info.filename or ':' in info.filename or rel.parts[0]!='Project-Relay-Local-v0.2.0-preview.3' or ((info.external_attr>>16)&0o170000)==0o120000:raise ValueError('Unsafe ZIP member')
        z.extractall(destination)
    package=destination/'Project-Relay-Local-v0.2.0-preview.3';sys.path.insert(0,str(package/'tools'))
    from verify_package import verify
    manifest=verify(package)
    if manifest['runtime_version']!=manifest['package_version'] or manifest['package_version']!='0.2.0-preview.3':raise ValueError('Version mismatch')
    print(json.dumps({'status':'PASS','local_zip_sha256':sums['Project-Relay-Local-v0.2.0-preview.3.zip'],'files':len(manifest['files'])+1,'package_version':manifest['package_version']},indent=2))
if __name__=='__main__':main()
