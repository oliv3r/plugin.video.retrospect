# python-qrcode (vendored)

Vendored copy of [python-qrcode](https://github.com/lincolnloop/python-qrcode)
v8.2 (commit `3704f57a`).

## Why vendored?

Retrospect needs QR code generation for the NLZIET device-flow login
dialog. python-qrcode is pure Python with no mandatory dependencies,
making it suitable for Kodi add-ons. Once a `script.module.qrcode` Kodi
add-on becomes available, this vendored copy should be replaced by a
dependency on that module.

## What is included?

Only the `qrcode/` package and `LICENSE` file are vendored. Tests,
documentation, packaging files, and other non-runtime files are excluded.

A thin wrapper at `resources/lib/qrcode/` bridges this vendored code
into Retrospect by injecting a stdlib-only PNG writer (replacing pypng)
and stubbing PIL.

## License

BSD — see [LICENSE](LICENSE).

## Updating

Replace the contents of this directory with the new release:

```sh
# Remove old vendored code (keep this README)
rm -rf resources/lib/python-qrcode/qrcode resources/lib/python-qrcode/LICENSE

# Clone and copy new version
git clone --depth 1 --branch <new-tag> https://github.com/lincolnloop/python-qrcode /tmp/python-qrcode
cp -r /tmp/python-qrcode/qrcode resources/lib/python-qrcode/
cp /tmp/python-qrcode/LICENSE resources/lib/python-qrcode/
rm -rf /tmp/python-qrcode

# Check for permission issues
find resources/lib/python-qrcode/qrcode -type f -executable
```
