# Third-party notes for the Obsidian plugin

The plugin in this directory (`src/`, `dist/main.js`, `styles.css`, `build.py`,
`tests/`, `demo/`) was written for this project and is released under the MIT
licence in [`LICENSE`](LICENSE) (same copyright holder as the project). It has no npm
dependencies and bundles no third-party library.

A few well-known algorithms, published formulas and conventions appear in the
code. None of them is a copy of a library, but they deserve credit:

| Where | What | Origin and licence |
| --- | --- | --- |
| `src/math.js` (`mat4Perspective`, `mat4LookAt`) | Standard perspective and look-at matrices (gluPerspective / gluLookAt, OpenGL clip space, column-major) | Written from the textbook definitions. The same conventions are used by [gl-matrix](https://github.com/toji/gl-matrix) (MIT, copyright Brandon Jones and Colin MacKenzie IV), credited here as the common reference. |
| `src/layout.js` (`rng`) | mulberry32 pseudo-random generator | Tommy Ettinger, dedicated to the public domain (CC0). |
| `src/layout.js`, `src/graph.js` (`hashString`) | FNV-1a string hash | Public domain algorithm (Fowler, Noll, Vo). |
| `src/layout.js` (`hilbertAxes`) | Index-to-coordinates transform of the 3D Hilbert curve | Written from the algorithm in J. Skilling, "Programming the Hilbert curve", AIP Conference Proceedings 707, 381 (2004). |
| `src/layout.js` (solver) | Force-directed placement with Barnes-Hut repulsion | The general technique of T. Fruchterman and E. Reingold (1991) and J. Barnes and P. Hut, Nature 324 (1986); the code was written for this project. |
| `src/shaders.js` (`QUAD_FS`) | 5-tap linear-sampled Gaussian blur weights and offsets | D. Rakos, "Efficient Gaussian blur with linear sampling" (2010). Binomial-derived constants. |
| `tests/palette-cvd.test.js` | Colour-vision deficiency simulation matrices (severity 1) and the CIEDE2000 colour difference | Published values from G. M. Machado, M. M. Oliveira and L. A. F. Fernandes, IEEE TVCG 15(6) (2009), and G. Sharma, W. Wu and E. N. Dalal, Color Research and Application 30(1) (2005), whose reference pairs the test also checks. |

The region colours in `src/regions.js` and `styles.css` were chosen for this
project by a search that keeps every pair apart under those simulations; they
are not taken from a published palette.

Obsidian itself (the `obsidian` module the plugin imports at runtime) is
provided by the host application and is not distributed here.
