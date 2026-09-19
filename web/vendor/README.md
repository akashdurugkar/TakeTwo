# Browser dependencies

Pinned browser distributions served locally (no runtime CDN requests):

- Marked 17.0.1: `marked.umd.js`, MIT license in `marked.LICENSE.md`.
- DOMPurify 3.3.1: `purify.min.js`, license in `dompurify.LICENSE`.
- pdfmake 0.3.11: `pdfmake.min.js` and `vfs_fonts.js`, MIT license in `pdfmake.LICENSE`.
	The bundled Roboto fonts are Apache-2.0 licensed; see `roboto.LICENSE`.
	Both bundles are loaded only when exporting a PDF, from this server, not a runtime CDN.

Sources: https://www.npmjs.com/package/marked, https://www.npmjs.com/package/dompurify,
and https://www.npmjs.com/package/pdfmake. PDF bundles come from the pinned npm package's `build/`
directory. Roboto license: https://github.com/googlefonts/roboto-2/blob/main/LICENSE.
Update pinned scripts and licenses together. Chat sanitizes Markdown output with an explicit tag and
attribute allowlist, removes image/media loading, and accepts only HTTP(S) links without credentials.
If either library is unavailable, replies fall back to plain text.