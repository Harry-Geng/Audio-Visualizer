# Representations of Music & How to Compare Them

A reference for thinking about the moment-similarity / taste-mapping engine
*abstractly and mathematically*. This is a design-space map, not a spec — it
exists so we can reason about **what** a "moment" should be and **what**
"similar" should mean before committing code.

---

## 0. The organizing idea

> A **representation** is a map `Φ: music → object` landing in some space `𝒳`
> that has structure. A **comparison** is a functional `d: 𝒳 × 𝒳 → ℝ` that
> respects that structure.

You have exactly **three knobs**, and almost every choice in music-IR is a
setting of these three:

1. **Object type** — what *kind* of mathematical object a moment becomes.
2. **Invariances** — which transformations you declare musically irrelevant and
   quotient out.
3. **Comparison functional** — the metric / divergence / kernel you put on the
   object, which must be compatible with (1) and respect (2).

The rest of this document expands each knob, then treats the stem-interaction
case (the core interest), the meta-question of comparing representations to
each other, taste as geometry, and the unifying kernel lens.

---

## 1. Knob 1 — What *type* of object is a "moment"?

The most consequential and least-examined choice. Today a moment is a **point in
ℝ^d** (concatenated MERT + handcrafted features). That is only the bottom rung:

| Object type | A moment *is*… | Native distance |
|---|---|---|
| **Point** `x ∈ ℝ^d` | one feature vector | cosine, Euclidean, Mahalanobis |
| **Set / distribution** `{x_t}` or `μ` | the *cloud* of its frame-vectors | Wasserstein (OT), MMD, Bhattacharyya, Fréchet |
| **Sequence / trajectory** `x(t)` | an ordered path through feature space | DTW, cross-correlation, Fréchet-curve |
| **Matrix / operator** | its covariance / cross-spectrum / dynamics | SPD-manifold metrics, Grassmann (subspace) |
| **Graph** | stems = nodes, interactions = edges | graph kernels, spectral distance |
| **Measure over a codebook** | a histogram over a moment-vocabulary | χ², Jensen–Shannon, OT |

### The key upgrade: point → distribution

A 5-second moment is really ~200 frame-vectors. Mean-pooling them to one point
discards the *shape* of the cloud. Keeping the distribution lets two moments be
"similar" when their **distributions of micro-textures** match:

- **2-Wasserstein / optimal transport**
  `W₂(μ,ν)² = inf_{γ ∈ Π(μ,ν)} 𝔼_{(x,y)~γ} ‖x − y‖²`
  — "minimum work to morph one moment's texture-cloud into the other's."
  Order-agnostic ⇒ robust to tempo/duration.
- **Fréchet distance** (the "FID" trick): approximate each cloud as a Gaussian
  `𝒩(m, C)`; then
  `d² = ‖m₁ − m₂‖² + Tr(C₁ + C₂ − 2 (C₁ C₂)^½)`.
  Closed-form, cheap, captures **mean + covariance** (average texture *and* how
  it varies within the moment).
- **MMD** (kernel two-sample distance):
  `MMD²(μ,ν) = ‖𝔼_x k(x,·) − 𝔼_y k(y,·)‖²_ℋ`.

---

## 2. Knob 2 — Which invariances do you quotient out?

"What counts as similar" is, formally, a choice of group. Let `G` be the group of
transformations declared **musically irrelevant**. The correct distance is the
**invariant (quotient) metric**:

> `d̄(x, y) = inf_{g ∈ G} d(x, g·y)`

i.e. distance in the quotient space `𝒳 / G`. Choosing `G` *is* choosing your
notion of taste-relevance. Candidates:

- **Time-warp (tempo)** — `G` = monotone time reparametrizations → DTW, or work
  in **beat-relative time** (segmenting on beats already quotients tempo).
- **Transposition (key)** — `G` = ℤ₁₂ cyclic shift of chroma → compare chroma
  **up to circular shift**: `d̄ = min over 12 rotations`. Makes "same
  progression, different key" identical.
- **Gain / loudness** — `G` = amplitude scaling → normalize or use
  scale-invariant features (cosine already kills global gain).
- **Channel / EQ** — `G` = filtering → harder; this is where *learned*
  invariances earn their keep.
- **Arrangement permutation** — relabeling stems.

Mental model: **the engine is really designing the equivalence relation "≈" on
moments**, and the metric is a smooth surrogate for it. Every facet weight is a
statement about which orbit-directions to ignore.

---

## 3. Knob 3 — The comparison functional (catalogue, matched to types)

- **Points** — cosine (angle = "same texture, any intensity"); Mahalanobis
  `d² = (x−y)ᵀ M (x−y)` with `M` learnable (see §6, taste-metric).
- **Distributions** — Wasserstein / MMD / Fréchet / Bhattacharyya /
  Jensen–Shannon.
- **Sequences** — DTW (warp-invariant), cross-correlation (lag-invariant),
  **soft-DTW** (differentiable ⇒ backprop-able).
- **Covariance / SPD matrices** — these do **not** live in a vector space; they
  live on a curved manifold. Use **log-Euclidean** `‖log C₁ − log C₂‖_F` or
  **affine-invariant** `‖log(C₁^{−½} C₂ C₁^{−½})‖_F`. (Critical for
  stem-interaction — §4.)
- **Subspaces** — span of a moment's top-k spectral components →
  **Grassmann manifold** / principal angles.
- **Parameter-free** — **Normalized Compression Distance**
  `NCD(x,y) = [C(xy) − min(C(x),C(y))] / max(C(x),C(y))`, `C` = any compressor.
  Assumption-light, philosophically clean ("similar = compresses well
  together"), a strong baseline.

---

## 4. The stem-interaction object (the core goal)

The heart of this project is *how the stems interact* — which is **not** a
property of any single stem but of the **relationships** between them. It
deserves its own representation, separate from content embeddings. A moment
becomes a **coupling structure** over the `K` stems.

### (a) As an SPD matrix
Build the `K × K` cross-stem matrix — correlation of envelopes, or the
**cross-spectral density** `Σ(f)` at each frequency (frequency-resolved
coupling: bass↔kick in the lows, vocal↔other in the mids). `Σ` is symmetric
positive-definite ⇒ compare two moments' coupling on the **SPD manifold**
(affine-invariant metric). This is the principled upgrade of hand-rolled
coupling scalars: instead of a few numbers, the whole covariance *geometry*,
compared on its proper curved space.

### (b) As a directed graph (who drives whom)
Coupling is often **asymmetric** — kick drives bass, not vice-versa. Capture
direction with:

- **Transfer entropy** `T_{A→B} = I(B_future ; A_past | B_past)` — model-free
  directed information flow.
- **Granger causality** — does A's past improve prediction of B?
- **Phase-locking value** — for envelope phases `φ_A, φ_B`,
  `PLV = |𝔼 e^{i(φ_A − φ_B)}| ∈ [0,1]` — how tightly locked the groove is,
  independent of amplitude.

A moment is then a **directed weighted graph** on stems; compare via graph
kernels or by vectorizing the (asymmetric) `K × K` matrix. **This is the
signature idea** — most systems never represent inter-stem causality. "Taste"
may be partly *which coupling graphs one gravitates to* (loose vs locked groove;
vocal-forward vs texture-forward).

### (c) Masking / spectral competition (operator view)
How much stem A's spectrum overlaps and masks B's — a transport/overlap
functional between their spectral densities (OT between spectra, or histogram
intersection).

---

## 5. Meta: comparing the *representations themselves*

How to know which `Φ` is better — well-posed, and usually skipped:

- **RSA / RDM** (representational similarity analysis) — for each
  representation, compute its `N × N` pairwise-distance matrix (the RDM). Two
  representations "agree" if their RDMs correlate (Spearman). Asks "do MERT and
  the hand-features see the same neighborhood structure?" *without* aligning
  coordinates.
- **CKA** (centered kernel alignment) —
  `CKA(X,Y) = ‖YᵀX‖_F² / (‖XᵀX‖_F · ‖YᵀY‖_F)`. The standard "are these two
  embedding spaces representing the same thing." Rotation/scale-invariant.
- **Procrustes / CCA** — best linear alignment; residual = how much one rep is
  just a rotation of another.
- **Task-based (the honest judge)** — a representation is good iff its geometry
  predicts something you care about. With **triplets** "moment *a* is more like
  *b* than *c*" (from your own ear), score each rep by triplet agreement, or by
  **neighborhood preservation** (trustworthiness / continuity). Turns "which
  representation" into a measurable horse-race.

---

## 6. Taste as geometry / topology (the end goal)

- **A taste = a density** `p_you(moment)` over moment-space; the library is
  samples from it. Find modes (your "moment-types"), measure concentration vs
  eclecticism (entropy of `p_you`), compare regions via OT.
- **Metric learning from your own groupings** — learn the Mahalanobis `M` (or
  facet-weights `w`) so moments you treat as similar are close. `M` *is* a
  computational model of your ear. With facet-kernels, **Multiple Kernel
  Learning** learns `k_you = Σ wᵢ kᵢ`; the weights reveal *what you attend to*
  (groove? timbre? harmony?).
- **Transition structure** — each song is a path through moment-types → a Markov
  chain. Taste = preferred **transitions** (how you like sound to *move*),
  compared via the chains' spectra. Captures arrangement/dynamics that static
  embeddings miss.
- **Topology (TDA)** — persistent homology of the moment cloud; loops/voids may
  encode that a taste is a structured manifold, not a blob. Speculative but
  genuinely abstract.

---

## 7. The unifying lens: kernels / RKHS

Almost every comparison above can be written as a **kernel** `k(x,y)`. Once you
have kernels:

- **Composability** — `k = Σ wᵢ kᵢ` (facets) or `k = Π kᵢ` (conjunctions); sums
  and products of kernels are kernels. "Weight the facets" becomes principled
  kernel combination, and **learning `w` = learning taste**.
- **Everything for free** — kNN, clustering, SVM, Gaussian processes,
  kernel-PCA for the taste map — all operate on `k` alone, agnostic to whether a
  moment was a point, a distribution (MMD kernel), an SPD matrix (log-Euclidean
  kernel), or a graph (graph kernel).

> Clean mental model: **each representation contributes a kernel; taste is the
> learned combination of kernels; the taste-map is kernel-PCA / UMAP on the
> combined kernel.**

---

## 8. How this maps onto the current codebase

What exists today (see `moment_index.py`, `interactions.py`, `descriptors.py`,
`embeddings.py`):

- **Object type** = *point in ℝ^d*. Each facet (`emb_mix`, `emb_<stem>`,
  `interactions`, `descriptors`) is a mean-pooled vector per moment.
- **Invariances** = beat-relative segmentation (tempo), L2-normalized embeddings
  + z-scored handcrafted facets (gain/scale). No key (chroma-shift) invariance
  yet.
- **Comparison** = cosine per facet, combined as a **weighted sum** — i.e.
  already a (linear) **kernel combination** `Σ wᵢ kᵢ` with hand-set `wᵢ`. §7 says
  the natural next step is to *learn* `w`.
- **Stem interaction** = §4 in scalar form (a handful of coupling features in
  `interactions.py`). The richest upgrade path is §4(a)/(b): promote it to an
  SPD coupling matrix or a directed graph and compare on the proper manifold.

### Concrete upgrade paths, ranked by leverage
1. **Moment-as-distribution** (§1) — keep frame clouds; add a Fréchet/Wasserstein
   facet. Likely the biggest fidelity gain for least conceptual risk.
2. **Inter-stem SPD / coupling-graph** (§4) — the project's signature; turn
   coupling scalars into a manifold/graph object.
3. **Learn the facet weights** (§6/§7) — metric/kernel learning from your own
   triplets so the combination reflects your ear, not a guess.
4. **Representation horse-race** (§5) — RSA/CKA to check whether MERT and the
   hand-features even capture different geometry (decides what's worth keeping).

---

*Glossary of acronyms: OT = optimal transport; MMD = maximum mean discrepancy;
DTW = dynamic time warping; SPD = symmetric positive-definite; PLV =
phase-locking value; RSA = representational similarity analysis; RDM =
representational dissimilarity matrix; CKA = centered kernel alignment; CCA =
canonical correlation analysis; NCD = normalized compression distance; MKL =
multiple kernel learning; TDA = topological data analysis; RKHS = reproducing
kernel Hilbert space.*
