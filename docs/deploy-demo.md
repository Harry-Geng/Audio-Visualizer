# Publishing a hosted demo

A public, zero-install preview so friends can click a link and use the *real*
app — stems, scene, galaxy, similar, radio, text search — without installing
anything or owning a GPU.

It works by serving a small **pre-processed library**: a handful of songs already
run through the pipeline, so the host needs no GPU, downloads no separation
models, and never touches the "add music" path (it's disabled by `AV_DEMO=1`).

> **Licensing:** the demo streams the songs' audio publicly, so use **only music
> you may redistribute** — Creative Commons (e.g. [Free Music Archive](https://freemusicarchive.org),
> [ccMixter](http://ccmixter.org)) or royalty-free tracks. Keep the attribution
> the license requires. Do **not** put commercial/copyrighted songs in the demo.

## 1. Build the demo library (on your GPU machine)

Drop your CC songs into a folder, then:

```bat
.venv\Scripts\python build_demo_library.py --src .\demo_songs
```

This analyzes each song (HQ vocals + drum kit), then builds the CLAP text-search
embeddings and the galaxy layout, writing everything to `demo_library/`. Aim for
~8–15 songs across a few genres so the galaxy and similarity results feel alive.

## 2. Create a Hugging Face Space

1. Sign in at [huggingface.co](https://huggingface.co) → **New → Space**.
2. **SDK: Docker** (blank template), name it, keep it public.
3. It gives you a git repo, e.g. `https://huggingface.co/spaces/<you>/music-microscope`.

## 3. Push the app + demo library to the Space

The Space repo needs the demo image and its data. From a clone of the Space:

```bash
# copy the app files the Dockerfile expects
cp Dockerfile requirements-demo.txt \
   microscope.py config.py interactions.py descriptors.py embeddings.py \
   moments.py feature_extractor.py feature_writer.py moment_index.py \
   <space-clone>/
cp -r microscope_static demo_library <space-clone>/

cd <space-clone>
# FLAC stems are large — track them with Git LFS
git lfs install
git lfs track "demo_library/**"
git add .gitattributes .

# a Docker Space needs this front-matter at the top of README.md:
```

Create `README.md` in the Space repo starting with:

```yaml
---
title: Music Microscope
emoji: 🎧
colorFrom: blue
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
---
```

Then push:

```bash
git commit -m "Music Microscope demo"
git push
```

The Space builds the Docker image (a few minutes) and goes live at
`https://huggingface.co/spaces/<you>/music-microscope`. Share that link.

## Notes

- **First load** wakes the Space if it slept (free tier sleeps when idle).
- **CPU only:** everything the demo does (playback, spectrograms, similarity,
  galaxy, text search) runs fine on CPU because the heavy work is pre-computed.
- **Updating songs:** re-run `build_demo_library.py`, copy `demo_library/` over,
  `git push` the Space.
- **Other hosts:** any Docker host works (Render, Fly.io, Railway, a VPS). Point
  it at this `Dockerfile`; set no env vars beyond what the Dockerfile already
  sets. Render can build straight from a GitHub repo — just include
  `demo_library/` (via LFS) or fetch it at build time.
