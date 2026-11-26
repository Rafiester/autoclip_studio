"""Intelligent Auto Director & offline camera trajectory optimizer for Smart Reframing.

Strictly separates camera editorial decisions and kinematic crop generation from
face detection, face tracking, active speaker feature extraction, and FFmpeg rendering.

Core responsibilities:
- Reframing style presets (`natural`, `stable`, `dynamic`) with tunable temporal parameters.
- Shot-aware offline direction: partitions at exact original camera cuts so crop
  coordinates never interpolate across unrelated camera shots.
- Multi-person offline Viterbi / Dynamic Programming state selection with lookahead,
  turn-boundary awareness, confidence hysteresis, reaction-shot intelligence, and
  rapid A-B-A ping-pong suppression (while preserving genuine short responses).
- Same-subject kinematic trajectory smoothing with generous safe-framing dead zone,
  edge-safety margins, lookahead dropout backfill, and velocity/acceleration clamping.
- Different-subject clean cuts aligned to exact frame midpoints (zero empty-space pans).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


REFRAME_STYLES: tuple[tuple[str, str], ...] = (
    ("natural", "Natural (Balanced)"),
    ("stable", "Stable (Fewer Cuts)"),
    ("dynamic", "Dynamic (Responsive)"),
)
VALID_REFRAME_STYLES = frozenset(code for code, _ in REFRAME_STYLES)


@dataclass(frozen=True, slots=True)
class DirectorStyleConfig:
    name: str
    min_shot_duration: float       # Preferred minimum hold between artificial switches (s)
    confirm_window: float          # Temporal confirmation window for speaker turns (s)
    switch_cooldown: float         # Minimum cooldown after an artificial switch (s)
    lookahead_seconds: float       # Offline lookahead horizon before switching (s)
    dropout_tolerance: float       # Brief face dropout hold/bridge tolerance (s)
    switch_penalty: float          # Base DP transition penalty for artificial switches
    hysteresis_margin: float       # Extra utility bonus for incumbent speaker continuity
    dead_zone_ratio: float         # Fraction of crop_width where camera stays rock-steady
    safe_margin_ratio: float       # Minimum horizontal frame margin keeping face away from edges
    max_pan_speed_ratio: float     # Max same-subject pan speed as fraction of crop_width / s
    max_pan_accel_ratio: float     # Max same-subject pan acceleration as fraction of crop_width / s^2
    reaction_damp: float           # Multiplier damping non-speaking visual motion (nods/smiles)
    short_turn_override: float     # Dominance ratio allowing genuine short response to override min_shot


STYLE_PRESETS: dict[str, DirectorStyleConfig] = {
    "natural": DirectorStyleConfig(
        name="natural",
        min_shot_duration=2.4,
        confirm_window=0.55,
        switch_cooldown=1.25,
        lookahead_seconds=1.50,
        dropout_tolerance=0.55,
        switch_penalty=1.15,
        hysteresis_margin=0.06,
        dead_zone_ratio=0.14,
        safe_margin_ratio=0.11,
        max_pan_speed_ratio=0.22,
        max_pan_accel_ratio=0.45,
        reaction_damp=0.45,
        short_turn_override=1.85,
    ),
    "stable": DirectorStyleConfig(
        name="stable",
        min_shot_duration=3.2,
        confirm_window=0.70,
        switch_cooldown=1.60,
        lookahead_seconds=2.00,
        dropout_tolerance=0.65,
        switch_penalty=1.75,
        hysteresis_margin=0.09,
        dead_zone_ratio=0.18,
        safe_margin_ratio=0.10,
        max_pan_speed_ratio=0.16,
        max_pan_accel_ratio=0.30,
        reaction_damp=0.30,
        short_turn_override=2.25,
    ),
    "dynamic": DirectorStyleConfig(
        name="dynamic",
        min_shot_duration=1.6,
        confirm_window=0.40,
        switch_cooldown=0.90,
        lookahead_seconds=1.10,
        dropout_tolerance=0.45,
        switch_penalty=0.72,
        hysteresis_margin=0.04,
        dead_zone_ratio=0.10,
        safe_margin_ratio=0.12,
        max_pan_speed_ratio=0.30,
        max_pan_accel_ratio=0.65,
        reaction_damp=0.60,
        short_turn_override=1.55,
    ),
}


def get_style_config(style: str | None) -> DirectorStyleConfig:
    key = (style or "natural").strip().lower()
    return STYLE_PRESETS.get(key, STYLE_PRESETS["natural"])


@dataclass(slots=True)
class TrackObservation:
    """Single-frame observation of one tracked face for camera direction."""
    track_id: int
    x: float
    y: float
    w: float
    h: float
    speaking_conf: float
    lip_motion: float = 0.0
    det_score: float = 1.0
    is_visible: bool = True

    @property
    def cx(self) -> float:
        return self.x + self.w * 0.5

    @property
    def cy(self) -> float:
        return self.y + self.h * 0.5

    @property
    def area(self) -> float:
        return max(0.0, self.w * self.h)


@dataclass(slots=True)
class DirectorSample:
    """Time-stamped scene, audio, and tracked-face state for offline Auto Director."""
    rel_t: float
    is_cut: bool
    vad: float
    tracks: list[TrackObservation]
    saliency_x: float
    turn_boundary: bool = False  # True near transcript/VAD pause between utterances


@dataclass(slots=True)
class DirectedKeyframe:
    time: float
    left: float
    cut: bool = False


@dataclass(slots=True)
class DirectionStats:
    original_cuts: int = 0
    artificial_switches: int = 0
    midpoint_avoided_frames: int = 0
    median_shot_duration: float = 0.0
    crop_jitter_px_s: float = 0.0
    face_visibility_ratio: float = 0.0
    style_used: str = "natural"


def _clamp_left(center_x: float, crop_width: int, src_width: int) -> float:
    return max(0.0, min(float(max(0, src_width - crop_width)), center_x - crop_width * 0.5))


def _can_group_obs(tracks: list[TrackObservation], crop_width: int, *, margin_ratio: float = 0.78) -> tuple[bool, float]:
    if not tracks:
        return False, 0.0
    if len(tracks) == 1:
        return True, tracks[0].cx
    min_left = min(t.x for t in tracks)
    max_right = max(t.x + t.w for t in tracks)
    if (max_right - min_left) <= crop_width * margin_ratio:
        return True, (min_left + max_right) * 0.5
    return False, 0.0


def _trimmed_median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    n = len(ordered)
    if n >= 5:
        trim = max(1, n // 10)
        ordered = ordered[trim:n - trim] or ordered
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[mid]
    return 0.5 * (ordered[mid - 1] + ordered[mid])


def _safe_left_for_face(
    current_left: float,
    obs: TrackObservation,
    crop_width: int,
    src_width: int,
    *,
    dead_zone: float,
    safe_margin: float,
) -> tuple[float, bool]:
    """Return (desired_left, needs_move). Holds rock-steady inside safe zone; guarantees face never clips edge."""
    ideal = _clamp_left(obs.cx, crop_width, src_width)
    # Check whether the face bounding box is already safely inside the current crop window.
    margin = max(safe_margin, obs.w * 0.28)
    face_left = obs.x
    face_right = obs.x + obs.w
    safe_lo = current_left + margin
    safe_hi = current_left + crop_width - margin
    inside_safe_box = (face_left >= safe_lo) and (face_right <= safe_hi)

    if inside_safe_box and abs(ideal - current_left) <= dead_zone:
        return current_left, False

    if not inside_safe_box:
        # Must move at least enough to pull the face comfortably back inside the safe frame,
        # nudging smoothly toward the ideal composition.
        return ideal, True

    # Outside dead zone but still inside box: gently shift target toward ideal.
    return ideal, True


def _partition_into_shots(samples: list[DirectorSample]) -> list[list[DirectorSample]]:
    shots: list[list[DirectorSample]] = []
    for sample in samples:
        if sample.is_cut or not shots:
            shots.append([sample])
        else:
            shots[-1].append(sample)
    return shots


def _compute_temporal_evidence(
    shot: list[DirectorSample],
    style: DirectorStyleConfig,
    src_width: int,
    src_height: int,
) -> tuple[list[int], list[dict[int, float]], list[dict[int, TrackObservation]]]:
    """Aggregate lookahead-aware audio-visual speaking evidence per track across one camera shot."""
    n = len(shot)
    all_tids: list[int] = []
    tid_set: set[int] = set()
    obs_maps: list[dict[int, TrackObservation]] = []
    for s in shot:
        omap: dict[int, TrackObservation] = {}
        for obs in s.tracks:
            omap[obs.track_id] = obs
            if obs.track_id not in tid_set:
                tid_set.add(obs.track_id)
                all_tids.append(obs.track_id)
        obs_maps.append(omap)

    # Filter out very brief single-frame ghost tracks when persistent tracks exist in the shot.
    if len(all_tids) > 1 and n >= 4:
        counts = {tid: sum(1 for m in obs_maps if tid in m) for tid in all_tids}
        max_cnt = max(counts.values())
        min_req = 2 if max_cnt >= 3 else 1
        kept_tids = [tid for tid in all_tids if counts[tid] >= min_req]
        if kept_tids:
            all_tids = kept_tids

    evidence: list[dict[int, float]] = [{} for _ in range(n)]
    frame_area = max(1.0, float(src_width * src_height))

    for i, s in enumerate(shot):
        t_i = s.rel_t
        win_lo = t_i - 0.25
        win_hi = t_i + style.confirm_window

        # Gather window indices once
        win_indices: list[int] = []
        for j in range(i, -1, -1):
            if shot[j].rel_t < win_lo:
                break
            win_indices.append(j)
        for j in range(i + 1, n):
            if shot[j].rel_t > win_hi:
                break
            win_indices.append(j)

        raw_scores: dict[int, float] = {}
        for tid in all_tids:
            causal_sum = 0.0
            causal_w = 0.0
            future_sum = 0.0
            future_w = 0.0
            visible_count = 0
            area_sum = 0.0
            curr_obs = obs_maps[i].get(tid)
            curr_gate = 1.0 if s.vad >= 0.22 else style.reaction_damp
            curr_score = (curr_obs.speaking_conf * curr_gate) if curr_obs is not None else 0.0

            for j in win_indices:
                dt = abs(shot[j].rel_t - t_i)
                w = math.exp(-dt / max(0.20, style.confirm_window * 0.75))
                obs = obs_maps[j].get(tid)
                speech_gate = 1.0 if shot[j].vad >= 0.22 else style.reaction_damp
                score_j = (obs.speaking_conf * speech_gate) if obs is not None else 0.0
                if obs is not None:
                    visible_count += 1
                    area_sum += obs.area
                if shot[j].rel_t <= t_i:
                    causal_sum += w * score_j
                    causal_w += w
                if shot[j].rel_t >= t_i:
                    future_sum += w * score_j
                    future_w += w

            causal_mean = (causal_sum / max(1e-6, causal_w)) if causal_w > 0 else 0.0
            future_mean = (future_sum / max(1e-6, future_w)) if future_w > 0 else causal_mean
            # Lookahead confirms that speaking starting NOW is sustained (rejects brief spikes
            # while never jumping early BEFORE the challenger actually starts speaking!).
            confirmed_now = min(curr_score * 1.15 + 0.03, future_mean)
            base_score = 0.45 * causal_mean + 0.55 * confirmed_now
            vis_ratio = visible_count / max(1, len(win_indices))
            mean_area = (area_sum / max(1, visible_count)) if visible_count else 0.0
            # Framing quality prior: visible, well-sized face gets gentle tie-break stability.
            framing_prior = min(0.10, math.sqrt(mean_area / frame_area) * 0.30) * vis_ratio
            # Penalize tracks not currently visible at frame i (dropout tolerance handled cleanly).
            curr_vis_bonus = 0.04 if curr_obs is not None else -0.05
            raw_scores[tid] = max(0.0, base_score + framing_prior + curr_vis_bonus)

        evidence[i] = raw_scores

    return all_tids, evidence, obs_maps


def _optimize_multi_speaker_shot(
    shot: list[DirectorSample],
    all_tids: list[int],
    evidence: list[dict[int, float]],
    obs_maps: list[dict[int, TrackObservation]],
    style: DirectorStyleConfig,
    crop_width: int,
    *,
    mode: str,
) -> list[int | str | None]:
    """Offline Viterbi / Dynamic Programming optimal camera state sequence within one shot.

    States:
    - Each persistent track_id in `all_tids`
    - Optional `"group"` state if all visible faces safely fit inside the 9:16 crop in `auto` mode
    """
    n = len(shot)
    if n == 0:
        return []
    if not all_tids:
        return [None] * n

    if mode == "single_subject" or len(all_tids) == 1:
        # Pick the primary subject (largest median face area & persistence) and stay rock-steady on it.
        best_tid = max(
            all_tids,
            key=lambda tid: (
                sum(1 for m in obs_maps if tid in m),
                _trimmed_median([m[tid].area for m in obs_maps if tid in m]),
            ),
        )
        return [best_tid] * n

    # Check if group framing is ever feasible in auto mode
    allow_group = mode == "auto"
    states: list[int | str] = list(all_tids)
    if allow_group:
        any_group = any(
            len(s.tracks) >= 2 and _can_group_obs(s.tracks, crop_width)[0]
            for s in shot
        )
        if any_group:
            states.append("group")

    num_states = len(states)
    shot_end_t = shot[-1].rel_t

    # Precompute emission utility U[i][s_idx]
    emit = [[0.0] * num_states for _ in range(n)]
    for i, s in enumerate(shot):
        omap = obs_maps[i]
        ev = evidence[i]
        sorted_ev = sorted(ev.values(), reverse=True)
        top_ev = sorted_ev[0] if sorted_ev else 0.0
        second_ev = sorted_ev[1] if len(sorted_ev) >= 2 else 0.0
        # Detect overlapping speech / group laughter (two tracks nearly tied under active speech).
        overlapping = s.vad >= 0.28 and second_ev >= top_ev * 0.78 and second_ev >= 0.08

        for si, st in enumerate(states):
            if st == "group":
                visible_tracks = [omap[tid] for tid in all_tids if tid in omap]
                fits, _ = _can_group_obs(visible_tracks, crop_width)
                if not fits or len(visible_tracks) < 2:
                    emit[i][si] = -2.0  # Infeasible: NEVER midpoint-average distant faces!
                else:
                    # Prefer group framing during silence/pause or genuine overlapping banter.
                    if s.vad < 0.25 or overlapping:
                        emit[i][si] = top_ev + 0.06
                    else:
                        emit[i][si] = top_ev * 0.85
            else:
                tid = int(st)
                u = ev.get(tid, 0.0)
                # Slight penalty if track is currently missing at frame i
                if tid not in omap:
                    u -= 0.14
                # Bonus for being clearly dominant speaker during speech
                if s.vad >= 0.22 and u >= top_ev - 1e-6 and top_ev > second_ev * 1.15:
                    u += 0.08
                emit[i][si] = u

    # Viterbi DP (maximize cumulative utility - transition penalties)
    dp = [[-1e9] * num_states for _ in range(n)]
    back = [[0] * num_states for _ in range(n)]

    # Initial frame utility (with lookahead bonus over first 1.0s so shot opens on the true speaker!)
    for si, st in enumerate(states):
        look_u = 0.0
        look_cnt = 0
        for j in range(min(n, 8)):
            if shot[j].rel_t - shot[0].rel_t <= max(0.8, style.confirm_window):
                look_u += emit[j][si]
                look_cnt += 1
        dp[0][si] = emit[0][si] + 0.6 * (look_u / max(1, look_cnt))

    for i in range(1, n):
        t_i = shot[i].rel_t
        remaining = max(0.0, shot_end_t - t_i)
        is_turn_boundary = shot[i].turn_boundary or (shot[i].vad < 0.22) or (shot[i - 1].vad < 0.22)

        for cur in range(num_states):
            best_val = -1e9
            best_prev = 0
            cur_st = states[cur]
            for prev in range(num_states):
                prev_st = states[prev]
                if cur == prev:
                    # Incumbent continuity hysteresis bonus
                    val = dp[i - 1][prev] + emit[i][cur] + style.hysteresis_margin * 0.25
                else:
                    # Compute contextual switch penalty
                    pen = style.switch_penalty
                    # 1. Offline lookahead: discourage switching right before shot ends!
                    if remaining < min(1.2, style.lookahead_seconds * 0.75):
                        pen *= 1.85
                    # 2. Conversational boundary alignment: cheaper to cut on natural turn boundaries/pauses.
                    if is_turn_boundary:
                        pen *= 0.72
                    # 3. If previous track disappeared (exceeded dropout), allow switch cleanly.
                    if prev_st != "group" and int(prev_st) not in obs_maps[i]:
                        # Check if missing for >= dropout_tolerance
                        miss_dur = 0.0
                        for k in range(i - 1, -1, -1):
                            if int(prev_st) in obs_maps[k]:
                                miss_dur = t_i - shot[k].rel_t
                                break
                        else:
                            miss_dur = t_i - shot[0].rel_t
                        if miss_dur >= style.dropout_tolerance:
                            pen *= 0.35
                    # 4. Infeasible group state cannot be chosen
                    if emit[i][cur] <= -1.5:
                        pen += 10.0
                    val = dp[i - 1][prev] + emit[i][cur] - pen

                if val > best_val:
                    best_val = val
                    best_prev = prev
            dp[i][cur] = best_val
            back[i][cur] = best_prev

    # Backtrack optimal state sequence
    best_last = max(range(num_states), key=lambda si: dp[n - 1][si])
    seq_idx = [0] * n
    seq_idx[n - 1] = best_last
    for i in range(n - 2, -1, -1):
        seq_idx[i] = back[i + 1][seq_idx[i + 1]]

    chosen: list[int | str | None] = [states[idx] for idx in seq_idx]

    # Post-DP Editorial Pass: suppress rapid A-B-A ping-pong & enforce cooldown while preserving genuine short turns.
    chosen = _editorial_cleanup_sequence(shot, chosen, evidence, obs_maps, style)
    return chosen


def _editorial_cleanup_sequence(
    shot: list[DirectorSample],
    seq: list[int | str | None],
    evidence: list[dict[int, float]],
    obs_maps: list[dict[int, TrackObservation]],
    style: DirectorStyleConfig,
) -> list[int | str | None]:
    """Eliminate rapid A-B-A ping-pong and trivial micro-shots while preserving genuine short speaker turns."""
    if len(seq) < 3:
        return seq

    result = list(seq)
    for _pass in range(3):
        # Build contiguous runs: (start_idx, end_idx_exclusive, state)
        runs: list[tuple[int, int, int | str | None]] = []
        r_start = 0
        for i in range(1, len(result) + 1):
            if i == len(result) or result[i] != result[r_start]:
                runs.append((r_start, i, result[r_start]))
                r_start = i

        if len(runs) <= 1:
            break

        changed = False
        for ri, (s0, s1, st) in enumerate(runs):
            dur = (shot[min(len(shot) - 1, s1)].rel_t if s1 < len(shot) else shot[-1].rel_t) - shot[s0].rel_t
            is_middle_aba = (
                0 < ri < len(runs) - 1
                and runs[ri - 1][2] == runs[ri + 1][2]
                and runs[ri - 1][2] is not None
            )
            min_needed = style.min_shot_duration if is_middle_aba else style.switch_cooldown
            if dur >= min_needed:
                continue

            # Determine neighbor state to consider collapsing into
            neighbor = runs[ri - 1][2] if ri > 0 else runs[ri + 1][2]
            if neighbor is None or neighbor == "group" or st == "group":
                continue

            # Check if neighbor is even visible during this run (cannot collapse into invisible face!)
            neighbor_vis = sum(1 for k in range(s0, s1) if int(neighbor) in obs_maps[k]) / max(1, s1 - s0)
            if neighbor_vis < 0.65:
                continue

            # Allow genuine short responses to override fixed duration threshold!
            if st is not None:
                st_mean = sum(evidence[k].get(int(st), 0.0) for k in range(s0, s1)) / max(1, s1 - s0)
                nb_mean = sum(evidence[k].get(int(neighbor), 0.0) for k in range(s0, s1)) / max(1, s1 - s0)
                mean_vad = sum(shot[k].vad for k in range(s0, s1)) / max(1, s1 - s0)
                genuine_short_turn = (
                    dur >= style.confirm_window
                    and mean_vad >= 0.30
                    and st_mean >= max(0.14, nb_mean * style.short_turn_override)
                )
                if genuine_short_turn:
                    continue

            # Collapse brief ping-pong / interruption into neighbor state
            for k in range(s0, s1):
                result[k] = neighbor
            changed = True
            break

        if not changed:
            break

    return result


def _generate_run_keyframes(
    shot: list[DirectorSample],
    s0: int,
    s1: int,
    state: int | str | None,
    obs_maps: list[dict[int, TrackObservation]],
    src_width: int,
    crop_width: int,
    style: DirectorStyleConfig,
    *,
    is_cut_start: bool,
    cut_time_override: float | None = None,
    fallback_left: float,
) -> tuple[list[DirectedKeyframe], int]:
    """Generate rock-steady, eased, acceleration-limited crop keyframes for one continuous subject run."""
    run_len = s1 - s0
    if run_len <= 0:
        return [], 0

    # 1. Collect raw target observations for this run (with lookahead backfill for brief start/mid dropouts!)
    raw_obs: list[TrackObservation | None] = [None] * run_len
    raw_centers: list[float | None] = [None] * run_len
    visible_frames = 0

    for offset in range(run_len):
        idx = s0 + offset
        s = shot[idx]
        if state == "group":
            vis = [o for o in s.tracks if o.is_visible]
            fits, gcx = _can_group_obs(vis, crop_width)
            if fits and vis:
                min_x = min(o.x for o in vis)
                max_x = max(o.x + o.w for o in vis)
                min_y = min(o.y for o in vis)
                max_y = max(o.y + o.h for o in vis)
                raw_obs[offset] = TrackObservation(
                    -1, min_x, min_y, max_x - min_x, max_y - min_y, 1.0,
                )
                raw_centers[offset] = gcx
                visible_frames += 1
        elif state is not None:
            obs = obs_maps[idx].get(int(state))
            if obs is not None:
                raw_obs[offset] = obs
                raw_centers[offset] = obs.cx
                visible_frames += 1

    # Lookahead backfill & dropout interpolation within this same shot run!
    # Eliminates mid-shot dropout wander (e.g., speaker looking down briefly at shot start).
    known_indices = [k for k, c in enumerate(raw_centers) if c is not None]
    if known_indices:
        # Backfill leading dropout from first known position in this shot
        first_k = known_indices[0]
        for k in range(0, first_k):
            raw_centers[k] = raw_centers[first_k]
            raw_obs[k] = raw_obs[first_k]
        # Fill trailing dropout from last known position in this shot
        last_k = known_indices[-1]
        for k in range(last_k + 1, run_len):
            raw_centers[k] = raw_centers[last_k]
            raw_obs[k] = raw_obs[last_k]
        # Linearly bridge internal dropouts
        for a_idx, b_idx in zip(known_indices, known_indices[1:]):
            if b_idx - a_idx <= 1:
                continue
            ca = raw_centers[a_idx]
            cb = raw_centers[b_idx]
            assert ca is not None and cb is not None
            for k in range(a_idx + 1, b_idx):
                frac = (k - a_idx) / float(b_idx - a_idx)
                raw_centers[k] = ca + (cb - ca) * frac
                raw_obs[k] = raw_obs[a_idx] if frac < 0.5 else raw_obs[b_idx]
    else:
        # No face detected anywhere in this shot: use stable median saliency across the shot.
        med_sal = _trimmed_median([shot[s0 + k].saliency_x for k in range(run_len)])
        for k in range(run_len):
            raw_centers[k] = med_sal

    # 2. Shot-level steady anchor: start on the trimmed median of the opening ~1.5s (or full short shot)
    # so the shot opens dead-on composition without initial jitter!
    init_window = [
        _clamp_left(raw_centers[k], crop_width, src_width)  # type: ignore[arg-type]
        for k in range(run_len)
        if (shot[s0 + k].rel_t - shot[s0].rel_t) <= 1.8
    ]
    anchor_left = _trimmed_median(init_window) if init_window else fallback_left

    # Verify anchor doesn't clip the initial face
    first_obs = raw_obs[0]
    dead_zone = crop_width * style.dead_zone_ratio
    safe_margin = crop_width * style.safe_margin_ratio
    if first_obs is not None:
        checked_left, _ = _safe_left_for_face(
            anchor_left, first_obs, crop_width, src_width,
            dead_zone=dead_zone, safe_margin=safe_margin,
        )
        # Only pull anchor if it actually clips the face
        margin = max(safe_margin, first_obs.w * 0.28)
        if first_obs.x < anchor_left + margin or (first_obs.x + first_obs.w) > anchor_left + crop_width - margin:
            anchor_left = checked_left

    # 3. Check if the ENTIRE run fits comfortably inside a single rock-steady locked tripod frame!
    # (True for most studio podcast close-ups where the host talks/laughs in their chair!)
    full_median_left = _trimmed_median([
        _clamp_left(c, crop_width, src_width) for c in raw_centers if c is not None
    ])
    all_fit_locked = True
    for k in range(run_len):
        obs = raw_obs[k]
        cx = raw_centers[k]
        if obs is not None:
            margin = max(safe_margin, obs.w * 0.25)
            if obs.x < full_median_left + margin or (obs.x + obs.w) > full_median_left + crop_width - margin:
                all_fit_locked = False
                break
        if cx is not None and abs(_clamp_left(cx, crop_width, src_width) - full_median_left) > dead_zone * 1.15:
            all_fit_locked = False
            break

    start_t = cut_time_override if cut_time_override is not None else shot[s0].rel_t
    if all_fit_locked:
        # Cinema-grade locked-off tripod framing: zero camera wobble!
        return [DirectedKeyframe(round(start_t, 3), round(full_median_left, 2), cut=is_cut_start)], visible_frames

    # 4. Eased, velocity- and acceleration-limited kinematic tracker (same subject moves beyond safe zone).
    max_speed = crop_width * style.max_pan_speed_ratio
    max_accel = crop_width * style.max_pan_accel_ratio
    current_left = anchor_left
    velocity = 0.0
    prev_t = start_t
    kfs: list[DirectedKeyframe] = [
        DirectedKeyframe(round(start_t, 3), round(current_left, 2), cut=is_cut_start)
    ]

    for k in range(1, run_len):
        t = shot[s0 + k].rel_t
        dt = max(0.04, t - prev_t)
        obs = raw_obs[k]
        cx = raw_centers[k]
        if obs is not None:
            desired_left, needs_move = _safe_left_for_face(
                current_left, obs, crop_width, src_width,
                dead_zone=dead_zone, safe_margin=safe_margin,
            )
        elif cx is not None:
            ideal = _clamp_left(cx, crop_width, src_width)
            needs_move = abs(ideal - current_left) > dead_zone
            desired_left = ideal if needs_move else current_left
        else:
            desired_left, needs_move = current_left, False

        if not needs_move:
            # Smoothly damp residual pan velocity to zero (eased stop).
            dec = max_accel * dt * 1.6
            if abs(velocity) <= dec:
                velocity = 0.0
            else:
                velocity -= math.copysign(dec, velocity)
            current_left = max(0.0, min(float(max(0, src_width - crop_width)), current_left + velocity * dt))
        else:
            error = desired_left - current_left
            # Critically-damped target velocity (smooth ease-in / ease-out, zero overshoot).
            target_vel = max(-max_speed, min(max_speed, error * 1.45))
            dv = target_vel - velocity
            max_dv = max_accel * dt
            velocity += max(-max_dv, min(max_dv, dv))
            step = velocity * dt
            # Prevent overshoot past desired_left
            if (error > 0 and step > error) or (error < 0 and step < error):
                step = error
                velocity = 0.0
            current_left = max(0.0, min(float(max(0, src_width - crop_width)), current_left + step))

        kfs.append(DirectedKeyframe(round(t, 3), round(current_left, 2), cut=False))
        prev_t = t

    return kfs, visible_frames


def direct_single_stream(
    samples: list[DirectorSample],
    src_width: int,
    src_height: int,
    crop_width: int,
    *,
    mode: str = "auto",
    style: str = "natural",
    source_fps: float = 25.0,
) -> tuple[list[DirectedKeyframe], DirectionStats]:
    """Run offline Auto Director over an entire clip and return frame-accurate keyframes + editorial stats."""
    cfg = get_style_config(style)
    stats = DirectionStats(style_used=cfg.name)
    fallback_left = _clamp_left(src_width * 0.5, crop_width, src_width)
    if not samples:
        return [DirectedKeyframe(0.0, round(fallback_left, 2), cut=False)], stats

    half_frame = 0.5 / max(10.0, min(120.0, source_fps))
    shots = _partition_into_shots(samples)
    stats.original_cuts = max(0, len(shots) - 1)

    all_keyframes: list[DirectedKeyframe] = []
    shot_durations: list[float] = []
    total_visible = 0
    total_samples = len(samples)

    for shot_idx, shot in enumerate(shots):
        all_tids, evidence, obs_maps = _compute_temporal_evidence(shot, cfg, src_width, src_height)

        # Count midpoint-avoided multi-face frames
        for s in shot:
            if len(s.tracks) >= 2 and not _can_group_obs(s.tracks, crop_width)[0]:
                stats.midpoint_avoided_frames += 1

        chosen = _optimize_multi_speaker_shot(
            shot, all_tids, evidence, obs_maps, cfg, crop_width, mode=mode,
        )

        # Split chosen states into contiguous subject runs inside this shot
        runs: list[tuple[int, int, int | str | None]] = []
        r0 = 0
        for i in range(1, len(chosen) + 1):
            if i == len(chosen) or chosen[i] != chosen[r0]:
                runs.append((r0, i, chosen[r0]))
                r0 = i

        for run_idx, (s0, s1, st) in enumerate(runs):
            is_original_cut = (shot_idx > 0 and run_idx == 0)
            is_artificial_switch = (run_idx > 0)
            if is_artificial_switch:
                stats.artificial_switches += 1

            # Exact cut alignment: anchor frame 0 at 0.000s, and align subsequent cuts to exact mid-frame!
            if shot_idx == 0 and run_idx == 0:
                cut_t_override: float | None = 0.0
            elif is_original_cut or is_artificial_switch:
                cut_t_override = max(0.0, shot[s0].rel_t - half_frame)
            else:
                cut_t_override = None

            run_kfs, vis_cnt = _generate_run_keyframes(
                shot, s0, s1, st, obs_maps, src_width, crop_width, cfg,
                is_cut_start=(is_original_cut or is_artificial_switch),
                cut_time_override=cut_t_override,
                fallback_left=fallback_left,
            )
            total_visible += vis_cnt

            # Ensure strictly monotonic keyframe timestamps
            for kf in run_kfs:
                if all_keyframes and kf.time <= all_keyframes[-1].time:
                    if kf.cut:
                        # Replace trailing non-cut sample at identical time with the cut
                        if not all_keyframes[-1].cut:
                            all_keyframes[-1] = kf
                        continue
                    continue
                all_keyframes.append(kf)

            # Record editorial shot duration
            run_start_t = shot[s0].rel_t
            if s1 < len(shot):
                run_end_t = shot[s1].rel_t
            elif shot_idx + 1 < len(shots):
                run_end_t = shots[shot_idx + 1][0].rel_t
            else:
                run_end_t = shot[-1].rel_t
            shot_durations.append(max(0.04, run_end_t - run_start_t))

    if not all_keyframes:
        all_keyframes = [DirectedKeyframe(0.0, round(fallback_left, 2), cut=False)]
    elif all_keyframes[0].time > 0.0:
        all_keyframes[0] = DirectedKeyframe(0.0, all_keyframes[0].left, cut=False)

    # Compute telemetry stats (median shot duration, same-shot crop jitter px/s, face visibility)
    if shot_durations:
        stats.median_shot_duration = round(_trimmed_median(shot_durations), 2)
    stats.face_visibility_ratio = round(total_visible / max(1, total_samples), 3)

    pan_motion = 0.0
    pan_time = 0.0
    for a, b in zip(all_keyframes, all_keyframes[1:]):
        if b.cut:
            continue
        dt = max(0.0, b.time - a.time)
        if dt > 0:
            pan_motion += abs(b.left - a.left)
            pan_time += dt
    stats.crop_jitter_px_s = round(pan_motion / max(0.1, pan_time), 2) if pan_time > 0 else 0.0

    return all_keyframes, stats
