# Third-party notices

## Star History renderer

The README Star History images are generated with the unmodified official renderer from [star-history/star-history](https://github.com/star-history/star-history), pinned to commit `608d0e39be441665c01cd6ac1fd01ca5ec1eaf39`. The renderer is downloaded into a disposable checkout during the statistics workflow. Its source is licensed under MIT:

```text
MIT License

Copyright (c) 2025 Star History

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Embedded xkcd Script font

The official renderer embeds **xkcd Script** (`xkcd-Script-Regular`) from [ipython/xkcd-font](https://github.com/ipython/xkcd-font). The font's embedded copyright notice states:

> Copyright (c) ipython/xkcd-font contributors, Creative Commons Attribution-NonCommercial 3.0 License

The embedded font remains under [CC BY-NC 3.0](https://creativecommons.org/licenses/by-nc/3.0/). It is included unmodified in the generated README chart SVGs. The project's Apache-2.0 license does not relicense this font or the upstream renderer.

## Node rendering dependencies

The private documentation tool under `tools/star-history/` pins its dependencies in `package-lock.json`. These packages retain their respective licenses; their license texts are included in the installed packages. They are not bundled into the application runtime.
