# Music Microscope — hosted demo image (CPU-only; serves a pre-processed library).
# Deploys cleanly to a Hugging Face Docker Space (see docs/deploy-demo.md).
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libsndfile1 && \
    rm -rf /var/lib/apt/lists/*

# HF Spaces run as a non-root user (uid 1000); install + write as that user so
# the model cache and any runtime temp files land in a writable HOME.
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    HF_HOME=/home/user/.cache/huggingface \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8
WORKDIR /home/user/app

# CPU-only PyTorch (small) + the viewer runtime deps
RUN pip install --no-cache-dir --user torch==2.6.0 \
        --index-url https://download.pytorch.org/whl/cpu
COPY --chown=user requirements-demo.txt .
RUN pip install --no-cache-dir --user -r requirements-demo.txt

# runtime modules only — NO separation stack (ingest is disabled in demo mode)
COPY --chown=user microscope.py config.py interactions.py descriptors.py \
     embeddings.py moments.py feature_extractor.py feature_writer.py \
     moment_index.py ./
COPY --chown=user microscope_static ./microscope_static
COPY --chown=user demo_library ./demo_library

# pre-bake the CLAP text encoder so text search is instant (no first-query wait)
RUN python -c "from transformers import ClapTextModelWithProjection, AutoTokenizer; \
    ClapTextModelWithProjection.from_pretrained('laion/clap-htsat-unfused'); \
    AutoTokenizer.from_pretrained('laion/clap-htsat-unfused')"

ENV AV_DEMO=1 \
    AV_LIBRARY_DIR=/home/user/app/demo_library
EXPOSE 7860
CMD ["python", "microscope.py", "--host", "0.0.0.0", "--port", "7860"]
