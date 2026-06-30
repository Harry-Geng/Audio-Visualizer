# Wrapper for the detached overnight batch (run via Scheduled Task so it survives
# the Claude session / client closing). Separation-only (AV_SKIP_LYRICS) so the
# aligner doesn't compete with the separation models for VRAM; lyrics are
# backfilled afterwards. Output appended to overnight_batch.log on the library drive.
$env:AV_LIBRARY_DIR  = "E:\microscope_library"
$env:AV_SKIP_LYRICS  = "1"
$env:PYTHONIOENCODING = "utf-8"
Set-Location -LiteralPath "C:\Users\harry\Audio-Visualizer"
& "C:\Users\harry\Audio-Visualizer\.venv\Scripts\python.exe" batch_spotify.py --from-file "playlist.csv" *>> "E:\microscope_library\overnight_batch.log"
