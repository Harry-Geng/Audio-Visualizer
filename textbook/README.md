# The Mathematics of the Music Microscope

Course-notes-style LaTeX book documenting the math running inside this
project — DSP, deep learning, high-dimensional geometry, and rendering —
with every result tied to the file and parameters where it executes
("In the code" boxes).

## Build

Requires a TeX distribution (MacTeX / TeX Live):

```sh
cd textbook
latexmk -pdf -output-directory=build main.tex
```

Output: `build/main.pdf`.

## Status

| Chapter | State |
|---|---|
| 1. From Sound to Vectors (sampling, DFT/FFT, STFT, autocorrelation pitch) | **written** |
| 2. Filters, Onsets, and Beats | outline |
| 3. Attention and Transformers | outline |
| 4. Self-Supervised Audio: MERT | outline |
| 5. Source Separation | outline |
| 6. Contrastive Embeddings: CLAP and the Vibe Axes | outline |
| 7. Maps of Timbre Space: PCA and UMAP | **written** |
| 8. Similarity Search and the Moment Index | outline |
| 9. The Mathematics of Rendering | outline |
| A. Equal Temperament and Chroma | **written** |

Conventions: amsthm environments numbered within chapters (shared counter for
theorem-like statements, separate for exercises); style lives in
`uwnotes.sty`. New chapters go in `chapters/` and get an `\input` line in
`main.tex`.
