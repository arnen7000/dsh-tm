#!/usr/bin/env python3
"""Archive a macOS app and verify its signature, runtime and ZIP round trip."""

import argparse
import hashlib
import os
from pathlib import Path
import plistlib
import re
import subprocess
import tempfile

ARCHES = {'arm64': 'arm64', 'x64': 'x86_64'}
BUNDLE_NAME = 'DSH-TM.app'
MIN_MACOS = (13, 5, 0)


def codesign_files(app):
    """Yield the physical Mach-O files. PyInstaller links Resources and Frameworks."""
    magic_numbers = {
        b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xce\xfa\xed\xfe', b'\xfe\xed\xfa\xce',
        b'\xca\xfe\xba\xbe', b'\xca\xfe\xba\xbf', b'\xbe\xba\xfe\xca', b'\xbf\xba\xfe\xca',
    }
    for path in sorted(app.rglob('*')):
        if path.is_file() and not path.is_symlink():
            with path.open('rb') as source:
                if source.read(4) in magic_numbers:
                    yield path


def verify_bundle(app, arch, signed):
    exe = app / 'Contents' / 'MacOS' / 'DSH-TM'
    runtime = app / 'Contents' / 'Resources' / 'runtime' / 'node' / 'bin'
    for path in app.rglob('*'):
        if path.is_symlink() and not path.resolve(strict=True).is_relative_to(app):
            raise ValueError(f'Bundle symlink points outside the app: {path}')
    with (app / 'Contents' / 'Info.plist').open('rb') as source:
        metadata = plistlib.load(source)
    if (metadata['CFBundleName'] != 'DSH-TM'
            or metadata['CFBundleDisplayName'] != 'DSH-TM'
            or metadata['CFBundleExecutable'] != exe.name
            or metadata['LSMinimumSystemVersion'] != '13.5'
            or metadata['CFBundleIdentifier'] != 'io.github.arnen7000.dsh-tm'):
        raise ValueError('Unexpected app name, executable, minimum macOS version or bundle identifier')
    binaries = list(codesign_files(app))
    for program in (exe, runtime / 'node'):
        if program.resolve() not in binaries:
            raise ValueError(f'Missing Mach-O executable: {program}')
    for binary in binaries:
        actual = subprocess.check_output(['/usr/bin/lipo', '-archs', binary], text=True, encoding="utf-8", errors="replace").strip()
        if actual != ARCHES[arch]:
            raise ValueError(f'{binary} has {actual}, expected native {ARCHES[arch]}')
        commands = subprocess.check_output(['/usr/bin/xcrun', 'vtool', '-show-build', binary], text=True, encoding="utf-8", errors="replace")
        minima = re.findall(
            r'(?:^\s*minos|LC_VERSION_MIN_MACOSX\n\s*cmdsize\s+\d+\n\s*version)'
            r'\s+(\d+\.\d+(?:\.\d+)?)', commands, re.MULTILINE,
        )
        for minimum in minima:
            version = tuple(map(int, minimum.split('.')))
            if version + (0,) * (3 - len(version)) > MIN_MACOS:
                raise ValueError(f'{binary} requires macOS {minimum}')
    subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', app], check=True)
    if signed:
        requirement = '=anchor apple generic and certificate leaf[field.1.2.840.113635.100.6.1.13] exists'
        subprocess.run(['/usr/bin/codesign', '--verify', '-R', requirement, app], check=True)
        subprocess.run(['/usr/bin/xcrun', 'stapler', 'validate', app], check=True)
        subprocess.run(['/usr/sbin/spctl', '--assess', '--type', 'execute', '--verbose=4', app], check=True)
    return runtime


def sign_bundle(app, identity, keychain, entitlements):
    """Sign Mach-O components and frameworks from the inside out."""
    node = (app / 'Contents' / 'Resources' / 'runtime' / 'node' / 'bin' / 'node').resolve(strict=True)
    args = ['/usr/bin/codesign', '--force', '--sign', identity, '--keychain', keychain,
            '--timestamp', '--options', 'runtime']
    for path in codesign_files(app):
        options = ['--entitlements', entitlements] if path == node else []
        subprocess.run([*args, *options, path], check=True)
    frameworks = sorted(app.rglob('*.framework'), key=lambda p: len(p.parts), reverse=True)
    for path in frameworks:
        if path.is_dir() and not path.is_symlink():
            subprocess.run([*args, path], check=True)
    subprocess.run([*args, app], check=True)
    subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', app], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--arch', choices=ARCHES, required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--output', type=Path, help='verified ZIP output path')
    action.add_argument('--sign', nargs=3, metavar=('IDENTITY', 'KEYCHAIN', 'ENTITLEMENTS'))
    parser.add_argument('--signed', action='store_true', help='require Developer ID and notarization')
    args = parser.parse_args()
    app = args.app.resolve()
    if app.name != BUNDLE_NAME or not app.is_dir():
        parser.error(f'--app must point to {BUNDLE_NAME}')
    if args.sign:
        sign_bundle(app, *args.sign)
        return

    output = args.output.resolve()
    if output.suffix != '.zip' or output.is_relative_to(app):
        parser.error('--output must be a ZIP path outside the app')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='dsh-tm-package-', dir=output.parent) as temp:
        work = Path(temp)
        archive = work / output.name
        extract = work / 'Extracted App'
        # ditto preserves the framework links, modes and stapled ticket.
        subprocess.run(['/usr/bin/ditto', '-c', '-k', '--keepParent', app, archive], check=True)
        subprocess.run(['/usr/bin/ditto', '-x', '-k', archive, extract], check=True)
        runtime = verify_bundle(extract / BUNDLE_NAME, args.arch, args.signed)
        env = {'PATH': '/usr/bin:/bin', 'HOME': str(work)}
        for name in ('node', 'npm', 'pnpm'):
            subprocess.run([runtime / name, '--version'], env=env, cwd=work, timeout=30, check=True)
        subprocess.run([runtime / 'node', '-e',
                        'new WebAssembly.Module(Uint8Array.of(0,97,115,109,1,0,0,0))'],
                       env=env, cwd=work, timeout=30, check=True)
        sha = hashlib.sha256()
        with archive.open('rb') as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                sha.update(chunk)
        checksum = work / (output.name + '.sha256')
        checksum.write_text(f'{sha.hexdigest()}  {output.name}\n', encoding='ascii')
        os.replace(archive, output)
        os.replace(checksum, output.with_suffix('.zip.sha256'))
    print(output)


if __name__ == '__main__':
    main()
