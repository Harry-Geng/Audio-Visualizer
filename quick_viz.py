import json
import numpy as np
import matplotlib.pyplot as plt

path = "12._brent_faiyaz_-_1_for_you._(spring_in_new_york)_[bonus_track]_features.json"
d = json.load(open(path))

fps = d["meta"]["fps"]
n_frames = d["meta"]["n_frames"]
t = np.arange(n_frames) / fps

beats = np.array(d["macro"]["beats"])  # in seconds

stems = ["drums", "bass", "vocals", "other"]
colors = ["#e74c3c", "#3498db", "#2ecc71", "#f39c12"]

fig, axes = plt.subplots(len(stems), 1, figsize=(16, 8), sharex=True)
fig.suptitle(d["meta"]["filename"], fontsize=13)

for ax, stem, color in zip(axes, stems, colors):
    rms = np.array(d["stems"][stem]["rms"])
    ax.fill_between(t, rms, alpha=0.6, color=color)
    ax.set_ylabel(stem, fontsize=9)
    ax.set_yticks([])
    for b in beats:
        ax.axvline(b, color="white", alpha=0.3, linewidth=0.5)

axes[-1].set_xlabel("Time (s)")
plt.tight_layout()
plt.show()
