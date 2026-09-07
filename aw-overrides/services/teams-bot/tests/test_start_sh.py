"""Static assertions on teams-bot's start.sh and Dockerfile.

THE CEILING, stated plainly: these are shell and config files. Nothing in this
suite executes them, so no test here proves the pod boots, that PulseAudio loads
a module, or that Edge launches. What they do prove is that the load-bearing
*text* — the framebuffer geometry, which sink the microphone is derived from,
the ordering of the mute relative to the node launch, and the Edge install —
still says what it must. That is worth having because every `pactl` call in
start.sh is `|| true`: a wrong argument fails silently at runtime and the only
place it is catchable before a live run is here.

RESOLVED VALUES, NOT SPELLINGS. This mirrors the discipline in Zoom's
`mute-guarantee.test.ts` (blocks M86/M125/M126) and it exists because of a real
survivor there: a guard matching the literal `master="${MIC_SILENCE_SINK}.monitor"`
could not see `MIC_SILENCE_SINK="$PULSE_SINK"`, an aliasing edit at the point of
DEFINITION that keeps the audited line byte-identical while pointing the bot's
microphone at the meeting audio. So every assertion about where audio goes
resolves the variable first, each resolved-value block carries a positive control
proving the resolver can actually go red, and the single-assignment check below is
the precondition that makes a position-blind resolver sound at all.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_PKG_DIR = Path(__file__).resolve().parent.parent
_START_SH = _PKG_DIR / "start.sh"
_DOCKERFILE = _PKG_DIR / "Dockerfile"

_ASSIGN_RE = re.compile(r'^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)="([^"]*)"')
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _code_only(sh: str) -> str:
    """Drop `#` comment lines, for NEGATIVE assertions on a shell source.

    Without this, a forbidden string mentioned in an explanatory comment (and
    start.sh's mic block deliberately names the forbidden master to explain it)
    would satisfy a "must not appear" check.
    """
    return "\n".join(
        line for line in sh.split("\n") if not line.lstrip().startswith("#")
    )


def _shell_assignments(sh: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in sh.split("\n"):
        t = line.strip()
        if t.startswith("#"):
            continue
        m = _ASSIGN_RE.match(t)
        if not m:
            continue
        name, value = m.group(1), m.group(2)
        # The `NAME="${NAME:-default}"` idiom: take the default, or resolution
        # would loop on itself.
        self_default = re.match(r"^\$\{" + name + r":-([^}]*)\}$", value)
        if self_default:
            value = self_default.group(1)
        out[name] = value
    return out


def _resolve(value: str, vars_: dict[str, str], depth: int = 0) -> str:
    if depth > 6:
        return value

    def sub(m: re.Match[str]) -> str:
        name = m.group(1) or m.group(2) or ""
        return vars_[name] if name in vars_ else m.group(0)

    nxt = _VAR_RE.sub(sub, value)
    return value if nxt == value else _resolve(nxt, vars_, depth + 1)


@pytest.fixture(scope="module")
def sh() -> str:
    assert _START_SH.is_file(), f"start.sh not found at {_START_SH}"
    return _START_SH.read_text()


@pytest.fixture(scope="module")
def dockerfile() -> str:
    assert _DOCKERFILE.is_file(), f"Dockerfile not found at {_DOCKERFILE}"
    return _DOCKERFILE.read_text()


@pytest.fixture(scope="module")
def shell_vars(sh: str) -> dict[str, str]:
    return _shell_assignments(sh)


# --- the resolver itself -----------------------------------------------------


def test_resolver_reads_the_default_idiom(shell_vars: dict[str, str]) -> None:
    # FIXTURE. If the resolver bound nothing, every resolved-value assertion
    # below would compare empty strings and pass for the wrong reason.
    assert shell_vars["PULSE_SINK"] == "teams_sink"
    assert shell_vars["MIC_SILENCE_SINK"] == "mic_silence_sink"
    assert shell_vars["BOT_MIC_SOURCE"] == "bot_mic"
    assert len({"teams_sink", "mic_silence_sink", "bot_mic"}) == 3


def test_resolver_expands_both_spellings() -> None:
    vars_ = {"A": "x", "B": "$A"}
    assert _resolve("${A}.monitor", vars_) == "x.monitor"
    assert _resolve("$A.monitor", vars_) == "x.monitor"
    assert _resolve("${B}", vars_) == "x"  # recursive
    assert _resolve("${UNSET}", vars_) == "${UNSET}"  # left alone, not blanked


def test_each_audited_name_is_assigned_exactly_once(sh: str) -> None:
    # The precondition for a position-blind resolver: `_resolve` lets the LAST
    # assignment win, while bash uses whatever was in force ABOVE the audited
    # line. Those are the same thing only if each name is assigned once. An
    # alias set before the pactl line and reset after it is precisely how the
    # echo-loop mutation slipped through in the Zoom harness.
    code = _code_only(sh)
    for name in ("PULSE_SINK", "MIC_SILENCE_SINK", "BOT_MIC_SOURCE"):
        n = len(
            [
                ln
                for ln in code.split("\n")
                if re.match(rf"^\s*(?:export\s+)?{name}=", ln)
            ]
        )
        assert n == 1, f"{name} is assigned {n} times; resolved-value audits need 1"


def test_the_assignment_counter_can_see_a_second_assignment(sh: str) -> None:
    # CONTROL for the test above: a counter that matched nothing would also
    # report a number, so prove it moves.
    code = _code_only(sh) + '\nMIC_SILENCE_SINK="mic_silence_sink"\n'
    n = len(
        [
            ln
            for ln in code.split("\n")
            if re.match(r"^\s*(?:export\s+)?MIC_SILENCE_SINK=", ln)
        ]
    )
    assert n == 2


# --- Xvfb geometry: 1920x1080, and it is not cosmetic ------------------------


def test_xvfb_is_1920x1080(sh: str, shell_vars: dict[str, str]) -> None:
    # index.ts launches the Teams context with `viewport: null` and the comment
    # "window fills the 1920x1080 Xvfb display", and Teams' whole
    # speaker-detection surface is DOM chrome that collapses at narrow widths.
    # Resolved, so a future `-screen 0 ${XVFB_GEOMETRY}` is still audited.
    lines = [
        ln
        for ln in _code_only(sh).split("\n")
        if ln.strip().startswith("Xvfb ") or " Xvfb " in ln
    ]
    assert len(lines) == 1, f"expected exactly one Xvfb launch, got {lines}"
    m = re.search(r"-screen\s+\d+\s+(\S+)", lines[0])
    assert m is not None, f"no -screen geometry on the Xvfb line: {lines[0]}"
    assert _resolve(m.group(1), shell_vars) == "1920x1080x24"


def test_xvfb_is_not_zoom_bots_720p(sh: str) -> None:
    assert "1280x720" not in _code_only(sh)


# --- the feedback-loop tripwire ---------------------------------------------


def test_exactly_one_remap_source_line(sh: str) -> None:
    # A second remap-source could reintroduce the loop while the first stays
    # correct and every assertion below keeps passing.
    assert len(_remap_lines(sh)) == 1


def _remap_lines(sh: str) -> list[str]:
    return [
        ln
        for ln in sh.split("\n")
        if "module-remap-source" in ln and not ln.lstrip().startswith("#")
    ]


def test_the_mic_master_resolves_to_the_dedicated_silent_sink(
    sh: str, shell_vars: dict[str, str]
) -> None:
    line = _remap_lines(sh)[0]
    m = re.search(r'master="([^"]*)"', line)
    assert m is not None, f"remap line has no master= argument: {line}"
    resolved = _resolve(m.group(1), shell_vars)
    assert resolved == "mic_silence_sink.monitor"


def test_the_mic_master_is_never_the_playback_sink_monitor(
    sh: str, shell_vars: dict[str, str]
) -> None:
    # THE MOST IMPORTANT ASSERTION IN THIS FILE. A microphone mastered on
    # ${PULSE_SINK}.monitor would let the bot retransmit the whole meeting back
    # into the meeting.
    line = _remap_lines(sh)[0]
    m = re.search(r'master="([^"]*)"', line)
    assert m is not None
    resolved = _resolve(m.group(1), shell_vars)
    assert resolved != f"{shell_vars['PULSE_SINK']}.monitor"
    assert _resolve("${MIC_SILENCE_SINK}", shell_vars) != _resolve(
        "${PULSE_SINK}", shell_vars
    )
    # Literal spellings too, on the remap line only so a comment cannot satisfy
    # or defeat it.
    for forbidden in (
        "${PULSE_SINK}.monitor",
        "$PULSE_SINK.monitor",
        "teams_sink.monitor",
    ):
        assert forbidden not in line


def test_an_aliased_silent_sink_would_be_caught(sh: str) -> None:
    # POSITIVE CONTROL. The two assertions above must be capable of failing;
    # this is the exact edit that survived a literal-only guard in the Zoom
    # harness.
    aliased = _shell_assignments(
        sh.replace(
            'MIC_SILENCE_SINK="mic_silence_sink"', 'MIC_SILENCE_SINK="$PULSE_SINK"'
        )
    )
    assert _resolve("${MIC_SILENCE_SINK}.monitor", aliased) == "teams_sink.monitor"
    assert _resolve("${MIC_SILENCE_SINK}", aliased) == _resolve(
        "${PULSE_SINK}", aliased
    )


# --- mute ordering and the mute targets -------------------------------------


def test_the_source_mute_happens_before_the_node_launch(sh: str) -> None:
    # entrypoint.sh records that muting the sink alone is insufficient because
    # "the remap source still passes a low-level signal to WebRTC, which Teams'
    # VAD interprets as speech". The very first getUserMedia must therefore
    # already be reading a muted source.
    code = _code_only(sh)
    src_mute = code.find('pactl set-source-mute "$BOT_MIC_SOURCE" 1')
    node = code.find("node /app/dist/docker.js")
    assert src_mute > -1, "the SOURCE is not muted (muting only the sink is not enough)"
    assert node > -1, "the node launch is missing"
    assert src_mute < node


def test_the_feed_sink_is_muted_too(sh: str) -> None:
    assert 'pactl set-sink-mute "$MIC_SILENCE_SINK" 1' in _code_only(sh)


def test_every_mute_target_resolves_to_the_mic_path(
    sh: str, shell_vars: dict[str, str]
) -> None:
    # Resolved, so a mute retargeted by aliasing at the definition is caught.
    assert _resolved_args(sh, shell_vars, "set-source-mute") == ["bot_mic"]
    assert _resolved_args(sh, shell_vars, "set-sink-mute") == ["mic_silence_sink"]


def _resolved_args(sh: str, vars_: dict[str, str], verb: str) -> list[str]:
    out: list[str] = []
    for line in _code_only(sh).split("\n"):
        if f"pactl {verb}" not in line:
            continue
        m = re.search(rf'pactl {verb}\s+"?([^"\s]+)"?', line)
        if m:
            out.append(_resolve(m.group(1), vars_))
    return out


# --- the default sink must never be the silent mic feed ---------------------


def test_the_silent_sink_is_never_made_the_default_sink(
    sh: str, shell_vars: dict[str, str]
) -> None:
    defaults = _resolved_args(sh, shell_vars, "set-default-sink")
    assert defaults, "no default sink is set at all"
    assert all(v == shell_vars["PULSE_SINK"] for v in defaults)
    assert shell_vars["MIC_SILENCE_SINK"] not in defaults


def test_the_default_source_resolves_to_the_bot_mic(
    sh: str, shell_vars: dict[str, str]
) -> None:
    assert _resolved_args(sh, shell_vars, "set-default-source") == [
        shell_vars["BOT_MIC_SOURCE"]
    ]


def test_the_default_sink_is_assigned_after_the_silent_sink_is_loaded(sh: str) -> None:
    # module-switch-on-connect promotes a newly appearing sink to default in some
    # PulseAudio configurations, so the explicit assignment has to come last.
    code = _code_only(sh)
    mic_sink_load = code.find('sink_name="$MIC_SILENCE_SINK"')
    default_sink = code.rfind('pactl set-default-sink "$PULSE_SINK"')
    assert mic_sink_load > -1 and default_sink > -1
    assert default_sink > mic_sink_load


# --- the pod-log verdict line ------------------------------------------------


def test_the_mic_state_helper_is_teams_named_and_reports_four_states(sh: str) -> None:
    # The helper must distinguish "absent" from "unmuted": one predicate for both
    # is what made an earlier Zoom pod log carry no information in any direction.
    assert "teams_mic_state()" in sh
    assert "zoom_mic_state" not in sh
    for state in ("absent", "muted", "unmuted", "unknown"):
        assert re.search(rf"echo {state}\b", sh), f"no `echo {state}` branch"


def test_operator_warning_strings_name_teams_not_zoom(sh: str) -> None:
    """Every WARNING an operator reads in a teams-bot pod log must say Teams.

    This file is otherwise a deliberate verbatim copy of zoom-bot's `start.sh`, so
    that `diff zoom-bot/start.sh teams-bot/start.sh` stays mechanically meaningful.
    These two strings are the ONE intended exception: an on-call engineer reading
    "Zoom" in a Teams pod's boot log mid-incident is a worse cost than a two-line
    diff. Nothing pinned that exception when it was made, and reverting both lines
    to Zoom's wording left the suite fully green (found by mutation) — precisely the
    accidental revert a future re-copy from zoom-bot's template would cause.

    Scoped to emitted lines only: the COMMENTS in start.sh still say Zoom where they
    recount Zoom's 2026-09-02 incident, because that history is correctly Zoom's.
    """
    emitted = [ln for ln in sh.splitlines() if re.match(r"\s*(echo|printf)\b", ln)]
    assert emitted, "no emitted lines found — the extraction regex is wrong"

    offenders = [ln.strip() for ln in emitted if "Zoom" in ln or "zoom" in ln]
    assert (
        not offenders
    ), "teams-bot would log Zoom's name to an operator: " + "; ".join(offenders)

    # Positive control: the two retargeted warnings must actually be present and
    # say Teams, so this test cannot pass by the strings having been DELETED.
    assert "Teams will not join audio for SENDING" in sh
    assert "Teams will see no microphone" in sh


def test_the_verified_muted_line_is_present(sh: str) -> None:
    # The one line an operator greps to know whether the fix is in force. The
    # live-verification checklist names this exact string.
    assert "bot mic VERIFIED muted at the capture level" in sh
    assert "MIC FIX NOT IN FORCE" in sh


# --- Dockerfile: apt packages, Edge, and the copied paths -------------------


def _apt_installed_packages(dockerfile: str) -> set[str]:
    """Collect the package names actually passed to `apt-get install`.

    NOT a substring search, and the difference is not academic. An earlier
    version of the Edge assertion below was `"microsoft-edge-stable" in
    dockerfile`, and deleting the whole `apt-get install ... microsoft-edge-stable`
    line left it GREEN — because the name still occurs in three explanatory
    comments and in the `/etc/apt/sources.list.d/microsoft-edge-stable.list`
    filename. The mutation harness caught that as a survivor. So this walks
    install invocations and their line continuations and yields package tokens
    only, which is the thing being claimed.
    """
    packages: set[str] = set()
    in_install = False
    for raw in dockerfile.split("\n"):
        line = raw.strip()
        if line.startswith("#"):
            continue
        if line.startswith("&&") or line.startswith("|"):
            in_install = False
        if "apt-get install" in line:
            in_install = True
            line = line.split("apt-get install", 1)[1]
        elif not in_install:
            continue
        body = line.rstrip("\\").strip()
        for token in body.split():
            if token.startswith("-") or token in {"&&", "|", ">", "apt-get", "install"}:
                continue
            packages.add(token)
        if not raw.rstrip().endswith("\\"):
            in_install = False
    return packages


def test_the_package_extractor_reads_install_lines_not_comments(
    dockerfile: str,
) -> None:
    # FIXTURE + CONTROL for the two tests below. It must find a package that is
    # unambiguously installed, and must NOT be satisfied by the sources.list
    # filename that defeated the earlier substring assertion.
    packages = _apt_installed_packages(dockerfile)
    assert "tini" in packages
    assert "pulseaudio" in packages
    assert not any(p.endswith(".list") for p in packages), packages
    assert not any("sources.list.d" in p for p in packages), packages


def test_notetaker_common_is_installed_with_ignore_installed(
    dockerfile: str,
) -> None:
    """The local-package install must carry --ignore-installed.

    The v1.56.0-noble base ships pip 25.3, which REFUSES to uninstall an
    apt-managed package (no RECORD file). pydantic==2.5.0 pulls a newer
    typing_extensions than the apt one, so a plain install dies with
    `uninstall-no-record-file`.

    This is a BUILD-TIME-ONLY failure, invisible to every other test in this
    suite, and it actually happened: teams-bot was copied from zoom-bot's
    Dockerfile, which predates the meet-bot fix (vexa-fork cc9e758b), so the
    first real build of teams-bot:v0.1.0 failed at exactly this line after
    ~220s of work.
    """
    line = next(
        (
            ln
            for ln in dockerfile.splitlines()
            if ln.startswith("RUN pip install") and "./notetaker-common" in ln
        ),
        None,
    )
    assert line is not None, "no `RUN pip install ... ./notetaker-common` line found"
    assert "--ignore-installed" in line, (
        "notetaker-common must be installed with --ignore-installed or the build "
        f"fails with uninstall-no-record-file; got: {line}"
    )


def test_aw_integration_is_installed_WITHOUT_ignore_installed(
    dockerfile: str,
) -> None:
    """And aw-integration must NOT carry it — the opposite mistake.

    aw-integration depends on notetaker-common, a LOCAL package on no index.
    --ignore-installed would make pip re-resolve that dependency from PyPI and
    fail with "No matching distribution for notetaker-common". So this is not a
    case of "add the flag everywhere": the two lines must differ, and a
    well-meaning tidy-up that unifies them breaks the build the other way.
    """
    line = next(
        (
            ln
            for ln in dockerfile.splitlines()
            if ln.startswith("RUN pip install") and "./aw-integration" in ln
        ),
        None,
    )
    assert line is not None, "no `RUN pip install ... ./aw-integration` line found"
    assert "--ignore-installed" not in line, (
        "aw-integration must NOT use --ignore-installed; pip would re-resolve "
        f"notetaker-common from PyPI and fail. Got: {line}"
    )


def test_dockerfile_installs_ffmpeg(dockerfile: str) -> None:
    # The WebM -> s16le/16 kHz/mono transcode is the PRIMARY Teams audio path:
    # _convert_container_to_pcm shells out to `ffmpeg` for every session. Nothing
    # else in the package declares that dependency, and a missing binary fails
    # only at the very end of a real meeting — the pipeline raises
    # `ffmpeg exited rc=...` after the audio has already been captured, which is
    # the most expensive moment possible to discover it.
    assert "ffmpeg" in _apt_installed_packages(dockerfile)


def test_dockerfile_installs_microsoft_edge_from_the_microsoft_repo(
    dockerfile: str,
) -> None:
    # index.ts launches Teams with `channel: 'msedge'` first; without Edge the
    # bot silently runs the bundled-Chromium fallback, which is not the path
    # upstream developed Teams against.
    assert "packages.microsoft.com/keys/microsoft.asc" in dockerfile
    assert "packages.microsoft.com/repos/edge stable main" in dockerfile
    # The install itself, not a mention of the name (see _apt_installed_packages).
    assert "microsoft-edge-stable" in _apt_installed_packages(dockerfile)


def test_dockerfile_asserts_edge_at_the_path_playwright_looks_for(
    dockerfile: str,
) -> None:
    # playwright-core resolves `channel: 'msedge'` to this fixed path on Linux.
    # Running --version at build time turns a broken install into a failed build
    # instead of a silent runtime fallback.
    assert "/opt/microsoft/msedge/msedge --version" in dockerfile


def test_dockerfile_copies_teams_bot_sources_only(dockerfile: str) -> None:
    # A copied Dockerfile that still COPYs zoom-bot's files would build a
    # teams-bot image running zoom-bot's sidecar and 720p start.sh — and would
    # look entirely fine until the first live run.
    copies = [ln for ln in dockerfile.split("\n") if ln.startswith("COPY ")]
    for name in ("aw_output_hook.py", "knock_delay.py", "start.sh"):
        matching = [ln for ln in copies if f"services/teams-bot/{name}" in ln]
        assert len(matching) == 1, f"{name} is not COPYed from services/teams-bot/"
    assert not [ln for ln in copies if "services/zoom-bot/" in ln]
    assert not [ln for ln in copies if "services/meet-bot/" in ln]


def test_dockerfile_build_hint_names_the_teams_bot_image(dockerfile: str) -> None:
    assert "teams-bot/Dockerfile" in dockerfile
    assert "voyantt-consultancy-services-llp/teams-bot" in dockerfile
