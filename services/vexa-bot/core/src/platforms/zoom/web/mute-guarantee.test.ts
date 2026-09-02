/**
 * Zoom recorder-bot mute guarantee — unit tests for both layers.
 *
 * Run: npx tsx services/vexa-bot/core/src/platforms/zoom/web/mute-guarantee.test.ts
 *
 * Layer 1 (readZoomMicState / selectZoomMicToggle): vocabulary-tolerant reading
 * of the in-meeting mic control. Layer 2 (silenceOutboundAudioTracks): the
 * DOM-independent, track-level silence guarantee.
 *
 * The FIRST fixture below is the real live DOM value from 2026-09-01
 * (aria-label="audio"). The shipped code compared aria-label with exact string
 * equality against 'Mute'/'Unmute', so a suite that only exercised those two
 * spellings would have been fully green while the bot sat unmuted in front of
 * participants for an entire meeting. That case is why this file exists.
 *
 * Matches alone-timer.test.ts / pcm-chunker.test.ts's standalone-harness
 * convention (no vitest).
 */

/**
 * MUTATION MANIFEST — every behaviour below was verified to FAIL when broken.
 * Reproduce one by making the edit in prepare.ts (or selectors.ts) and re-running
 * this file; the named test must go RED. Tests tagged [Mnn] map to an id here.
 *
 *  M1  prepare.ts readZoomMicState: `label.includes('unmute')` -> `label === 'unmute'`
 *      (and the same for 'mute')  === THE ORIGINALLY SHIPPED BUG
 *      red: '"unmute my microphone" => muted' (+3 more)
 *  M2  readZoomMicState: swap the two blocks so `includes('mute')` is tested first
 *      red: '"Unmute" => muted (substring order: unmute tested before mute)' (+4)
 *  M3  selectors.ts: swap zoomMicUnmutedClassHints / zoomMicMutedClassHints test order
 *      red: 'icon class "SvgAudioUnmuted" => unmuted' (+2)
 *  M4  readZoomMicState: add `if (probe.ariaPressed === 'false') return unmuted`
 *      red: 'aria-pressed="false" => unknown, NEVER unmuted'
 *  M5  readZoomMicState: non-toggle branch returns `unmuted` instead of `not-mute-toggle`
 *      red: 'LIVE FIXTURE aria-label="audio" reads not-mute-toggle' (+8)
 *  M6  selectZoomMicToggle: also accept `not-mute-toggle` as selectable
 *      red: 'LIVE FIXTURE aria-label="audio" is not selected as a toggle' (+4)
 *  M7  normaliseZoomMicText: drop `.toLowerCase()`            red: 15 tests
 *  M8  silenceOutboundAudioTracks: iterate getReceivers() instead of getSenders()
 *      red: 'INCOMING audio track is never touched'
 *  M9  silenceOutboundAudioTracks: remove the voiceAgentEnabled early return
 *      red: 'voiceAgentEnabled => outbound audio stays ENABLED' (+2)
 *  M10 silenceOutboundAudioTracks: drop `track.kind !== 'audio'` guard
 *      red: 'outbound VIDEO track is left alone' (+1)
 *  M11 silenceOutboundAudioTracks: add `break` to the per-peer catch
 *      red: 'a throwing peer does not stop later peers' (+1)
 *  M12 silenceOutboundAudioTracks: remove the `connectionState === 'closed'` skip
 *      red: 'closed peer connection is skipped'
 *  M13 silenceOutboundAudioTracks: set registryPresent = true before the Array.isArray check
 *      red: 'absent registry => registryPresent false' (+2)
 *  M14 silenceOutboundAudioTracks: delete `track.enabled = false`, keep the counter
 *      red: 'GUARANTEE: outbound AUDIO track is disabled' (+2)
 *  M15 readZoomMicState: move the aria-pressed check below the class hints
 *      red: 'aria-pressed="true" outranks a contradicting unmuted class hint'
 *  M16 readZoomMicState: move the non-toggle label check back above aria-pressed
 *      red: 'LIVE SHAPE aria-label="audio" + aria-pressed="true" => muted' (+2)
 *  M17 lockTrack: delete `track.enabled = false` so defineProperty runs FIRST
 *      red: '[M17] REAL audio state is stopped' (+8)   <- the ordering trap
 *  M18 lockTrack: delete the whole Object.defineProperty(...) seal
 *      red: '[M18] GUARANTEE: setting enabled=true does NOT unmute' (+7)
 *  M19 installOutboundAudioLockInPage: delete `lockTrack(args[0])` in the addTrack patch
 *      red: '[M19] ZERO-WINDOW: track added via addTrack is disabled' (+2)
 *  M20 replaceTrack patch: call orig BEFORE lockTrack
 *      red: '[M20] it was already disabled BEFORE the original replaceTrack ran'
 *  M21 installOutboundAudioLockInPage: remove the voiceAgentEnabled early return
 *      red: '[M21] voice agent: outbound audio stays ENABLED' (+3)
 *  M22 stepZoomMuteWatcher: `if (reading !== 'unmuted')` -> `if (reading === 'muted')`
 *      red: "[M22] 'unknown' => never clicks" (+2)
 *  M23 stepZoomMuteWatcher: disable the `consecutiveUnmuted < confirmations` guard
 *      red: '[M23] 1st unmuted reading => no click yet' (+2)
 *  M24 stepZoomMuteWatcher: disable the cooldown guard
 *      red: '[M24] second click refused inside the cooldown window' (+3)
 *  M25 getUserMedia patch: do not lockTrack the returned audio tracks
 *      red: '[M25] ZERO-WINDOW: mic track from getUserMedia is disabled'
 *  M26 readZoomMicState: delete the zoomNonMicLabelSubstrings guard entirely
 *      red: '[M26] "Ask to unmute" => not-mic-control' (+10)
 *  M27 readZoomMicState: move that guard BELOW the mute branches (W3's defect)
 *      red: '[M26] "Ask to unmute" => not-mic-control' (+10)
 *  M28 selectors.ts: replace the footer-scoped mute selectors with page-wide ones
 *      red: '[M28] every mute-vocabulary selector is scoped to the footer/toolbar'
 *  M28b selectors.ts: reintroduce a comma-list selector
 *      red: '[M28] no selector contains a comma list' (+2)
 *  M29 lockTrack: drop `state.errors++` from the catch
 *      red: '[M29] the sealing failure is counted (drives the guard re-sweep)'
 *  M30 startZoomMuteWatcher: delete `await revealZoomFooter(page)` (W4's defect —
 *      made the watcher permanently inert with the toolbar auto-hidden)
 *      red: '[M30] the watcher reveals the footer before probing'
 *  M30b startZoomMuteWatcher: delete the no-candidate log line
 *      red: '[M30] and it is LOGGED — an invisible no-op is the worst outcome'
 *
 *  M32 lockTrack: restore a WeakSet short-circuit in place of isSealed()
 *      (BLOCKER 1 — a de-sealed track was never re-sealed)
 *      red: '[M32] step 4: the re-install RE-SEALS and stops the audio again' (+2)
 *  M32b lockTrack: drop the `__vexaAudioLockGetter` marker on the getter
 *      red: '[M32] an intact seal is VERIFIED, not re-sealed' (+1)
 *  M34 installOutboundAudioLockInPage: remove the RTCPeerConnection ctor patch
 *      red: '[M34] the RTCPeerConnection constructor is patched' (+2)
 *  M34b ctor patch: write Meet's `__vexa_peer_connections` instead of the Zoom-local name
 *      red: '[M34] a ZOOM-LOCAL registry is created'
 *  M35 remove the addTransceiver patch
 *      red: '[M35] a track attached via addTransceiver is sealed at birth' (+1)
 *  M36 join.ts: delete the pre-navigation addInitScript arm (BLOCKER 2's timing)
 *      red: '[M36] join.ts arms the lock via addInitScript' (+1)
 *
 *  M37 parseZoomAudioLockEnv: flip the default to OFF (would silently ship an
 *      unprotected bot)   red: '[M37] DEFAULT: unset => seal ENABLED' (+6)
 *  M37b drop .toLowerCase() so opt-outs become case-sensitive
 *      red: '[M37] "OFF" => seal DISABLED' (+2)
 *  M37c remove 'disabled' from the opt-out list
 *      red: '[M37] "disabled" => seal DISABLED'
 *  M38 lockTrack: delete `if (!sealEnabled) return;` (seal applied even when OFF)
 *      red: '[M38] HONEST LIMIT: with the switch off an unmute CAN succeed'
 *  M38b delete the kill-switch early return before the at-birth patches
 *      red: '[M38] addTrack is NOT patched' (+1)
 *  M38c switch OFF also skips `enabled = false` (bot would be LIVE)
 *      red: '[M38] the track is STILL disabled (enabled=false is retained)'
 *  M39 make the voiceAgent bypass depend on the switch
 *      red: '[M21] voice agent: outbound audio stays ENABLED' (+5)
 *  M40 stop reporting seal-OFF mode in describeOutboundAudioLock
 *      red: '[M40] seal OFF: the description says so explicitly' (+1)
 *  M41 dead-code the decision log (`void 0 && log(...)`)
 *      red: '[M41] the decision is LOGGED at arm time (a real statement...)'
 *      NOTE: this SURVIVED a weaker `includes('log(')` guard; the assertion is
 *      now statement-position. Source guards are weak against clever mutations.
 *  M41b join.ts: hardcode `sealEnabled: true` instead of threading the switch
 *      red: '[M41] join.ts threads the switch into the page-load arm'
 *
 *  M42 silenceOutboundAudioTracks: read only `__vexa_peer_connections` again
 *      (the QA nit — the fallback sweep found nothing in the default config)
 *      red: '[M42] the ZOOM-LOCAL registry alone is enough for the sweep' (+4)
 *  M42b remove the `!peers.includes(peer)` dedup
 *      red: '[M42] a peer in both registries is counted ONCE' (+1)
 *  M43 join.ts preview: revert to `probe.ariaLabel === 'Mute'`
 *      red: '[M43] it clicks ONLY on a confident unmuted reading'
 *  M43b join.ts preview: click on a MUTED reading too (toggle hazard)
 *      red: '[M43] and explains why a muted reading is not clicked'
 *
 * ---- OBSERVABILITY + VERIFICATION batch, 2026-09-02 (Defects A and B) ----
 *
 *  M44 reportZoomAudioGuardTick: drop `audioSenders=${sweep.audioSendersFound} `
 *      from sweepDetail  red: '[M44] the heartbeat carries audioSenders' (+2)
 *  M44b hardwire `warn = true` — kills the healthy-tick CONTROL, without which
 *      every other warn assertion could pass vacuously
 *      red: '[M44] a healthy tick is informational, not a warning' (+1)
 *  M45 drop `lock.blockedUnmutes,` from the signature array
 *      red: '[M45] lock.blockedUnmutes is in the signature'
 *      (the same edit on any other field reddens that field's row)
 *  M45b `.join('|')` -> `.join('|') + String(Math.random())` — a signature that
 *      never repeats turns the heartbeat back into a 10s flood
 *      red: '[M45] an unchanged situation keeps the SAME signature'
 *  M46 null-lock branch `warn: true` -> `false`
 *      red: '[M46] an unreadable lock is a WARNING, not a silent return'
 *  M47 no-sweep branch reports `audioSenders=0` instead of `unreadable`
 *      red: '[M47] a missing sweep reads "unreadable", never 0' (+1)
 *  M48 delete the `!sweep.registryPresent` branch
 *      red: '[M48] an absent registry is named explicitly' (+1)
 *  M49 drop `sweep.tracksDisabled > 0 ||` from warn
 *      red: '[M49] a sweep that had to disable a track is a WARNING'
 *  M50 drop `lock.errors > 0 ||` from warn      red: '[M50] lock errors => warning'
 *  M50b drop `!lock.sealEnabled ||` from warn
 *      red: '[M50] seal disabled by the kill switch => warning on every heartbeat'
 *  M52 guard: restore `if (!lock) return;` and re-gate the sweep on
 *      `lock.errors > 0`  === DEFECT A, EXACTLY AS SHIPPED
 *      red: '[M52] every tick sweeps' (+1)
 *  M52b guard: `if (line) emit(line, report.warn);` -> `void line;`
 *      red: '[M52] and the collapsed line is actually LOGGED'
 *  M52c guard: delete the heartbeat.flush in stop()
 *      red: '[M52] stop() flushes, so the final suppressed run is not lost'
 *  M53 reportZoomMuteClick: drop the post-click reading from the confirmed line
 *      red: '[M53] and the reading taken AFTER it'
 *  M54 `input.after && input.after.reading.kind === 'muted'` -> `input.after`
 *      (any reading at all counts as a mute)
 *      red: '[M54] an unmuted re-read => verdict still-unmuted' (+7)
 *  M55 unreadable branch `warn: true` -> `false`
 *      red: '[M55] an unverifiable click is a warning'
 *  M56 drop `clicked ${target} ` from `trigger` — the clicked selector[index]
 *      was missing from the shipped log and could not be recovered
 *      red: '[M56] the clicked selector[index] is named' (x3 verdicts)
 *  M57 `priorIneffectiveClicks + 1` -> `priorIneffectiveClicks`
 *      red: '[M57] the first proven failure counts 1' (+2)
 *  M58 watcher: reinstate the unconditional `Mute watcher re-muted the bot`
 *      log  === DEFECT B, EXACTLY AS SHIPPED
 *      red: '[M58] the unconditional false-success line is GONE'
 *  M58b watcher: `verdict === 'still-unmuted'` -> `verdict !== 'muted'`
 *      red: '[M58] only a PROVEN failure is tallied'
 *  M58c watcher: pass `after: selection` (the TRIGGER reading) instead of the
 *      re-read — the exact "assertion pointed at the wrong state" shape
 *      red: '[M58] and `after` is a RE-READ of the post-click probe'
 *  M58d watcher: delete the post-click `await revealZoomFooter(page);` — a
 *      hidden toolbar probes as nothing, so every click would read unverifiable
 *      red: '[M58] the footer is re-revealed AFTER the click'
 *  M58e watcher: `verifyError = ...` -> `throw ve` in the verification catch
 *      red: '[M58] a failed verification is captured, not thrown to the
 *      generic handler'
 *  M58f watcher: afterDetail loses the verification-failure branch
 *      red: '[M58] and it is named in the report detail'
 *
 * ---- CANDIDATE DISCOVERY + structural discriminators, 2026-09-02 ----
 *
 * Root cause (from the parallel diagnosis): the element is Zoom's audio footer
 * control in a state where Zoom declines to label it Mute/Unmute. Evidence
 * ranks 1-2 found no mute vocabulary, so only the substring class hint fired —
 * a PRESENCE TEST read as if it were state. Two clicks landed on it (visible,
 * enabled, hit-testable, no interception, no Playwright actionability error) and
 * the reading did not move on the next 15s poll. The seal is exonerated: the
 * getUserMedia patch returns the SAME MediaStream, so it never blocked Zoom's
 * audio join. And the bot did not join unmuted — it read confidently muted for
 * the first ~3m45s. So the fix is neither "mute harder at join" nor "click
 * harder"; it is to VERIFY a click and use the result to find the right element.
 *
 *  M59 selectZoomMicToggle: key the rejection on `selector[index]` instead of
 *      elementKey — two selectors match the SAME button, so the loop re-clicks it
 *      red: '[M59] rejecting the ELEMENT skips it under EVERY selector' (+1)
 *  M59b delete the rejection skip
 *      red: '[M59] …' (+1) — and '[M60] the NEXT candidate is selected', which
 *      is what separates discovery from simply giving up
 *  M61 drop the `candidate.probe.elementKey &&` guard, so '' becomes rejectable
 *      red: '[M61] an empty key … does NOT suppress an unidentified candidate'
 *  M62 confirmed-mute branch also returns a rejectKey
 *      red: '[M62] a click that WORKED never rejects the element that worked'
 *  M62b unreadable branch returns a rejectKey (v1 of this edit was INEFFECTIVE
 *      — it added a duplicate object key that the later one overrode, and the
 *      suite stayed green; re-run as a real edit and caught)
 *      red: '[M62] an UNVERIFIABLE click does not reject'
 *  M63 `rejectKey: key || null` -> `key`
 *      red: '[M63] but an EMPTY key is never handed to the rejection set'
 *  M64 delete the visible-text mute vocabulary  === THE TIER THAT WOULD HAVE
 *      CAUGHT THE LIVE FAILURE      red: '[M64] label node "Mute" => unmuted' (+5)
 *  M64b text tier tests 'mute' before 'unmute'
 *      red: '[M64] label node "Unmute" => muted' (+3)
 *  M65 'Join Audio' no longer identifies the unjoined control
 *      red: '[M65] named explicitly, not lumped in with "unreadable"'
 *  M66 whole-element text outranks the label node
 *      red: '[M66] the label node wins over whole-element text' (+1)
 *  M67 promote the WHOLE text tier above aria-label (v1 promoted only the
 *      'unmute' branch — too weak to change the fixture's outcome, and it
 *      SURVIVED; re-run promoting the whole tier and caught)
 *      red: '[M67] aria-label outranks the label node when they disagree' (+1)
 *  M68 non-mic guard reverted to aria-label only — "Mute All" as TEXT would read
 *      unmuted and the watcher would mute every human in the meeting
 *      red: '[M68] "Mute All" via the label node => not-mic-control' (+3)
 *  M69 delete the icon whitelist tier
 *      red: '[M69] visible icon token "svgaudiounmuted" => unmuted' (+1)
 *  M70 a join-audio glyph no longer identifies the unjoined control
 *      red: '[M70] a join-audio glyph is not a mute toggle' (+1)
 *  M71 whitelist reads ALL descendants instead of only rendered ones
 *      red: '[M69] …' (+5), incl. '[M71] the VISIBLE whitelist beats a hidden hint'
 *  M72 delete the legacy substring tier — this is why it was KEPT below the
 *      whitelist rather than replaced: a whitelist-only reader goes blind on the
 *      next rename      red: 'icon class "SvgAudioUnmuted" => unmuted' (+3, incl.
 *      '[M72] a renamed glyph still reads via the substring fallback')
 *  M73 promote caret ABSENCE to a veto — the selectors are an unverified guess,
 *      and a veto on a selector that never matches makes the watcher permanently
 *      blind (the M30 failure)
 *      red: '[M73] caret ABSENT: reading unchanged — no veto' (+3)
 *  M73b drop the caret from the evidence
 *      red: '[M73] but presence is reported' (+1)
 *  M74 probe harvests ALL descendant classes as "visible"
 *      red: '[M74] visibleDescendantClassNames is filtered to RENDERED nodes'
 *  M75 element identity includes the class attribute — it CHANGES with the mic
 *      state, so a rejection keyed on it silently stops applying
 *      red: '[M75] identity NEVER includes the class attribute'
 *  M76 watcher stops passing the rejection set to the selection
 *      red: '[M76] and passes it to the selection'
 *  M76b watcher never retires what the report rejected
 *      red: '[M76] and retires whatever the report rejected'
 *
 * ---- ROOT CAUSE: no capture device + a genuine hands-off mode, 2026-09-02 ----
 *
 * The bot had NO MICROPHONE. aw-overrides/services/zoom-bot/start.sh created a
 * null SINK and no capture SOURCE, so Chromium had nothing to offer as a mic,
 * Zoom never joined audio for SENDING, audioSenders stayed 0, and Zoom had no
 * audio session for this participant — which is why the participant-list mute
 * state had nothing to describe and two landed clicks changed nothing. The seal
 * is exonerated: the getUserMedia patch returns the SAME MediaStream.
 *
 *  M77 empty zoomAudioLockNoneValues     red: '[M77] "none" => none' (+7)
 *  M77b mode parser drops .trim()/.toLowerCase()
 *      red: '[M77] "NONE" => none' (+1)
 *  M77c unknown values default to 'none' (would silently disarm on a typo)
 *      red: '[M77] DEFAULT: unset => on' (+6)
 *  M78 NOT A REAL INVARIANT — recorded rather than faked. Reordering the mode
 *      parser to delegate to the boolean FIRST is semantically EQUIVALENT,
 *      because 'none' is not in the boolean's opt-out list, so it SURVIVED.
 *      The property that actually matters is pinned behaviourally by test 90
 *      ('[M78] the old boolean parser reads "none" as ENABLED'): if anyone ever
 *      adds 'none' to that list, THAT test goes red. No mutation is claimed here.
 *  M79 lockTrack consults the switch BEFORE writing enabled=false — that would
 *      make 'off' identical to 'none' and destroy the isolation value of having
 *      both (v1 of this edit only added a comment and SURVIVED; recorded because
 *      a no-op edit is not evidence)
 *      red: 'GUARANTEE: outbound AUDIO track is disabled' (+12, incl.
 *      '[M79] mode off: and the unmute SUCCEEDS — so off is not hands-off')
 *  M80 isZoomAudioTouchAllowed uses parseZoomAudioLockEnv, which misreads 'none'
 *      as enabled — tracks would be touched in hands-off mode
 *      red: '[M80] and it is false ONLY in hands-off mode' (+1)
 *  M41(3) the HANDS OFF mode is not logged
 *      red: '[M41] ALL THREE modes are logged plainly'
 *  M41b(3) isZoomAudioSealEnabled re-reads the env instead of the memoised mode
 *      (two independent reads can disagree, and both would log)
 *      red: '[M41] the seal flag derives from the single memoised mode' (+1)
 *  M81 join.ts installs the page-load arm even in hands-off mode
 *      red: '[M81] join.ts gates the page-load arm on hands-off'
 *  M82 index.ts stops gating the sweep/lock/guard
 *      red: '[M82] and gates the track-touching steps on it' (+1)
 *  M83 the DOM half re-gated on touchAllowed — hands-off would lose the ONLY
 *      thing still maintaining the visual state. v1 of this edit wrapped the
 *      block and SURVIVED: every index-ordering assertion was unchanged, which
 *      is a REAL test weakness, not a bad edit. Fixed by asserting the gate is
 *      CLOSED before the DOM block and that nothing re-gates after it.
 *      red: '[M83] the mute watcher and the DOM mute are in a separate block'(+2)
 *  M84 the guard drops its own hands-off refusal
 *      red: '[M84] the guard refuses to arm in hands-off mode on its own account'
 *
 *  start.sh is a SHELL SCRIPT: nothing in this harness executes it and nothing
 *  simulates PulseAudio, so M85-M90 are STATIC assertions on the script text.
 *  Strictly weaker than behavioural proof and NOT offered as equivalent — they
 *  exist because the worst mistake available here is a one-word edit.
 *
 *  M85 delete the remap source  === THE ROOT CAUSE, RESTORED
 *      red: '[M85] start.sh creates a remap SOURCE' (+2)
 *  M86 remap the mic from ${PULSE_SINK}.monitor — THE ECHO LOOP: that monitor
 *      carries the meeting audio parecord records, so the bot could transmit the
 *      whole meeting back into the meeting
 *      red: '[M86] the mic is NOT derived from ${PULSE_SINK}.monitor' (+1)
 *  M86b add a SECOND remap source from zoom_sink.monitor
 *      red: '[M86] exactly one remap-source line'
 *  M87 make the silent mic sink the default sink (Zoom would render the meeting
 *      into it — an EMPTY recording)
 *      red: '[M87] the silent mic sink is NEVER made the default sink' (+3)
 *  M87b MOVE the default-sink assignment before the silent sink is loaded, so
 *      module-switch-on-connect could steal the default (v1 ADDED a duplicate
 *      line that the later one overrode, and SURVIVED)
 *      red: '[M87] the recording sink is made default AFTER the silent sink'
 *  M88 drop the SOURCE mute, leaving only the sink mute — entrypoint.sh records
 *      that this is insufficient ("the remap source still passes a low-level
 *      signal to WebRTC")
 *      red: '[M88] the SOURCE is muted (not just the sink)'
 *  M88b MOVE the source mute after the node launch, so the first getUserMedia
 *      would NOT receive zeros (v1 re-inserted it just BEFORE node and SURVIVED)
 *      red: '[M88] and the source mute happens BEFORE node starts'
 *  M89 rename the mic source virtual_mic — services/tts-playback.ts runs
 *      `pactl set-source-mute virtual_mic 0` and would UNMUTE it
 *      red: '[M89] no EXECUTED line names virtual_mic' (+1)
 *  M90 rename $PULSE_SINK, breaking parecord's explicit --device
 *      red: '[M90] $PULSE_SINK is still zoom_sink by default'
 *
 * ---- CRITICAL: the post-click re-read was not pinned to the clicked element ----
 *
 * `after: selectZoomMicToggle(afterCandidates)` returned the FIRST CANDIDATE
 * THAT READ CONFIDENTLY out of a fresh, re-ordered set of up to 28 entries, and
 * the verdict was attributed to the click. False 'muted' would log "CONFIRMED
 * the bot muted after clicking" from a different element and never retire the
 * ineffective one; false 'still-unmuted' would RETIRE a working control for the
 * whole session. The identical defect was also present in
 * ensureZoomMutedInMeeting's re-read (join-time path), where it could return
 * muted:true. Both are now keyed on probe.elementKey.
 *
 *  M91 readZoomMicCandidateByKey reverts to the old fresh-selection strategy
 *      === THE FIX REMOVED, exactly as shipped
 *      red: '[M91] verdict is unreadable — NOT muted from a different element'
 *      (+15, incl. '[M91] the word CONFIRMED never appears' and
 *      '[M92] the WORKING control is NOT retired')
 *  M91b drop the `if (!elementKey) return null;` guard
 *      red: '[M95] an empty key matches NOTHING, even a key-less candidate'
 *  M91c byKey filters out non-confident readings instead of returning them —
 *      the caller could then not say WHAT the clicked element read
 *      red: '[M95] a non-confident reading is still returned' (+3)
 *  M91d compare `candidate.selector` to the key instead of probe.elementKey
 *      (v1 added a widening disjunction that could never match a key and was
 *      therefore a NO-OP; it SURVIVED and is recorded as such)
 *      red: '[M92] verdict is muted — the clicked element did mute' (+10)
 *  M92(4) the muted verdict stops naming which element it describes
 *      red: '[M92] the line names the element the verdict describes'
 *  M96(4) the unreadable message stops naming the missing element
 *      red: '[M94] and the line says the element was not found'
 *  M97 the WATCHER re-read reverts to selectZoomMicToggle(afterCandidates)
 *      red: '[M97] and the fresh-selection strategy is GONE from the watcher'(+1)
 *  M98 ensureZoomMutedInMeeting's re-read reverts likewise
 *      red: '[M98] its post-click re-read is keyed to the clicked element' (+1)
 *
 * NOTE on the replaced guard: test 105 replaces an assertion that pinned
 * `after: verifyError ? null : selectZoomMicToggle(afterCandidates),` as
 * CORRECT. A source guard asserting a defect is true is worse than no guard —
 * it makes the bug look deliberate and survives review. The load-bearing
 * assertion is now the NEGATIVE one: the fresh-selection strategy must not
 * reappear on either re-read. Every FIXTURE in tests 99-101 additionally
 * asserts that the OLD strategy really would have produced the wrong verdict,
 * so none of those tests can pass vacuously.
 *
 * ---- CRITICAL 2 + the 9 warnings, 2026-09-02 (one coherent change) ----
 *
 * Critical 2: retirement rested on ONE reading at a fixed +750ms and was
 * permanent. Zoom's mute is a server-acked audio-session operation, not a CSS
 * toggle, so on a loaded pod the toolbar reflects it later than that — the
 * re-read sampled the PRE-click state and retired the only control that works.
 * Before retirement existed the same timing produced a harmless re-click loop.
 * `750` appeared exactly once in the tree, in the code, in no fixture.
 *
 *  M99  readZoomMuteVocabulary tests the ACTION word before the STATE word
 *       === W3, THE DANGEROUS ONE: "Muted" read as `unmuted` -> the watcher
 *       clicks -> a MUTED bot becomes UNMUTED
 *       red: '[M99] "muted" => muted' (+16)
 *  M99b remove only the 'muted' state branch    red: same family (+12)
 *  M100 the text tier bypasses the vocabulary reader (bare substring again)
 *       red: '[M100] labelText "Muted" => muted, NOT unmuted' (+4)
 *  M100b the aria-label tier bypasses it — the hazard existed on BOTH tiers
 *       red: '[M100] aria-label "Muted" => muted' (+1)
 *  M102 aria-pressed promoted back above the visible-text tier === W4
 *       red: 'aria-pressed="true" outranks a contradicting unmuted class hint'(+3)
 *  M103 the settle poll returns after ONE probe === CRITICAL 2 AS SHIPPED
 *       red: '[M103] the poll does NOT time out — the state arrived' (+7)
 *  M104 the deadline is ignored; only the iteration cap remains
 *       red: '[M103] FIXTURE: the old strategy takes exactly ONE reading' (+7)
 *  M104b/M104c the ITERATION CAP is removed, leaving only the deadline.
 *       FOUND BY THE HARNESS HANGING: with a frozen clock the poll never
 *       returned, so it killed the suite instead of failing it. Two fixes: the
 *       poll gained a hard cap (an unbounded loop inside a live bot's
 *       setInterval is not an acceptable failure mode for any edit to reach),
 *       and the test now yields a MACROTASK and races a timeout so it can
 *       OBSERVE non-termination instead of joining it.
 *       red: '[M104b] the poll TERMINATES even when the deadline can never fire'
 *  M105 the poll does not early-exit on a settled mute      red: (+5)
 *  M106 the poll reads a fresh selection instead of the clicked element
 *       (composes with Critical 1)   red: '[M106] the clicked element was never
 *       present, so there is NO reading' (+3)
 *  M107 a later absent probe erases an earlier confident reading
 *       red: '[M107] the earlier confident reading is retained'
 *  M108 retirement on the FIRST strike (corroboration removed)
 *       red: '[M108] the FIRST proven failure is only a strike' (+6)
 *  M109 strikes shared across keys instead of per key        red: (+3)
 *  M110 an empty key becomes retirable ('' is the ambient elementKey and would
 *       match every unidentified element)   red: (+2)
 *  M111 the absorbing check ignores whether anything is readable
 *       red: '[M111] ALL readable controls retired => ABSORBING'
 *  M111b the reset keeps the strikes, so the next failure retires instantly
 *       red: '[M111] and every strike, so discovery starts clean'
 *  M112 zero capture devices reported as if it were fine === W9
 *       red: '[M112] zero devices is named as the root cause'
 *  M112b an unrunnable send-side probe fabricates a zero     red: (+0)
 *  M113 mode off stops warning that tracksLocked is structurally 0 === Q5
 *  M114 the seal-off line returns to the `tracksDisabled` MISLABEL — it printed
 *       0 for tracks that WERE disabled                      red: (+2)
 *  M115 the shutdown flush hardcodes the informational level === W11
 *  M116 the path key loses its `path:` prefix === W6
 *  M117 the live-run order recommends 'none' again — 'none' refuses to arm the
 *       guard, so it blinds the instrument this work added
 *  M120/M121 THE SEND-SIDE CANARY: deleting the probe, and computing the line
 *       but never logging it, BOTH SURVIVED the suite — a real gap, since it is
 *       the only line reporting whether the start.sh fix is in force. Guards
 *       added (statement-position, plus brace-matched proof that it sits
 *       OUTSIDE the track-touching gate so it runs in every mode).
 *       red: '[M120] index.ts runs the send-side probe' /
 *            '[M121] and LOGS it — a computed-but-unlogged canary is no canary'
 *  M82(v2) the three track-touching calls MOVED BELOW the gate — the old
 *       ordering-only proof (`gateIdx < sweepIdx`) stayed green. Replaced with
 *       brace-matched containment plus controls that the matcher can fail.
 *       red: '[M82] and the guard is inside it' (+1)
 *  M86c ECHO: mic remapped from zoom_sink.monitor by literal name
 *  M86d ECHO BY ALIASING: `MIC_SILENCE_SINK="$PULSE_SINK"` — SURVIVED. The
 *       guard matched the LITERAL `master="${MIC_SILENCE_SINK}.monitor"`, which
 *       an alias leaves untouched while pointing the mic at the meeting audio.
 *       This is the one failure mode that puts the meeting back into the
 *       meeting. Fixed by RESOLVING shell variables and asserting the resolved
 *       master, with a positive control proving the resolver can catch it.
 *       red: '[M86] the RESOLVED mic master is the dedicated silent sink monitor'
 *  M86e the silent sink is never created                     red: (+0)
 *  M118 the mute verification is short-circuited to `if true` — SURVIVED. The
 *       guard matched the function DEFINITION, not the CALL: the same
 *       "identifier instead of call" mistake already recorded once in this file.
 *       red: '[M118] the gate is never short-circuited to a constant'
 *  M118b the unverifiable branch no longer unloads the source === W7
 *  M119 the contradicting "only hard guarantee" sentence returns === W8
 *
 * ---- SAFETY: the reference vs the referent, 2026-09-02 ----
 *
 * The echo-loop guard checked WHICH VARIABLE the mic's master names and never
 * WHAT THAT VARIABLE IS BOUND TO. `MIC_SILENCE_SINK="zoom_sink"` at the point of
 * DEFINITION leaves the remap line reading `master="${MIC_SILENCE_SINK}.monitor"`
 * verbatim, and the whole M86 family stayed green while the microphone was fed
 * from the monitor parecord records. The tripwire covered the edit a careless
 * REVIEWER would make (at the point of use) and missed the edit a careless
 * AUTHOR would make (at the point of definition). Note the asymmetry that gave
 * it away: BOT_MIC_SOURCE was pinned BY VALUE (M89); MIC_SILENCE_SINK was not
 * pinned by value anywhere.
 *
 * Fixed by RESOLVING shell variables (shellAssignments + resolveShell) and
 * asserting resolved values, which defeats every aliasing spelling at once
 * rather than enumerating spellings. A text-level guard keeps losing that race.
 *
 *  M125  ALIAS: `MIC_SILENCE_SINK="zoom_sink"`, remap line untouched
 *        red: '[M86] the RESOLVED mic master is the dedicated silent sink
 *        monitor' (+7). Both this and the `="$PULSE_SINK"` spelling are caught,
 *        each verified with the remap line proven unchanged.
 *  M125b ALIAS: `BOT_MIC_SOURCE="zoom_sink.monitor"` — the default CAPTURE
 *        device becomes a monitor, the echo loop by another route
 *        red: '[M125] and is never a .monitor device' (+2)
 *  M125c `set-default-sink "$MIC_SILENCE_SINK"` — Zoom renders the meeting into
 *        the silent sink and the recording is EMPTY
 *        red: '[M125] EVERY resolved set-default-sink argument is the recording
 *        sink' (+5)
 *  M125d the source mute is applied to the wrong device
 *        red: '[M125] the source mute is applied to the resolved bot mic' (+1)
 *  M125e only ONE null sink is created, so the mic feed IS the recording sink
 *        red: '[M125] exactly two null sinks are created' (+4)
 *
 * ---- QA hole 2: the icon-tier order could decide a dangerous reading ----
 *
 * Swapping the two whitelist lookups was behaviour-preserving for every
 * single-glyph fixture (exact-token matching has no containment problem to
 * order around), so it left the suite green — while with BOTH glyphs rendered
 * the order ALONE decided whether a MUTED bot read `unmuted` and got clicked.
 * Fixed by computing both lookups before either return and resolving the
 * conflict toward `muted`: the ordering dependency is REMOVED, not pinned.
 *
 *  M122  the whitelist conflict rule is disabled   red: '[M122] two
 *        contradicting VISIBLE glyphs resolve to muted' (+5)
 *  M122b the conflict resolves to UNMUTED (the direction that clicks)
 *        red: '[M122] it is never the direction that triggers a click' (+2)
 *  M123  the substring-tier conflict rule is disabled           red: (+1)
 *  M123b the per-token guard is dropped === THE M3 TRAP: 'svgaudiounmuted'
 *        contains 'muted', so a blob-level both-matched test is true for every
 *        unmuted-ONLY control and the bot would never mute itself
 *        red: 'icon class "SvgAudioUnmuted" => unmuted (NOT muted...)' (+9)
 *  M124  source guard: both lookups must precede the first return
 *
 * ---- The aliasing class RELOCATED one level up, 2026-09-02 ----
 *
 * Round 1: the guard matched a ${VAR} SPELLING and never read the definition.
 * Round 2 reads definitions — but shellAssignments is POSITION-BLIND (last
 * assignment wins), while bash uses whatever was in force ABOVE the audited
 * line. So the alias moved to a line the guard still never effectively reads:
 * the one that gets overwritten later.
 *
 * Fixed by asserting the PRECONDITION under which position-blindness is sound,
 * rather than making the resolver position-aware: if each audited name is
 * assigned exactly once in the executed code, last-wins IS the value in force
 * at every line. Two lines, and strictly stronger than a positional parser —
 * it fails loudly the moment the precondition stops holding.
 *
 *  M126  ALIAS in force AT the remap, reset AFTERWARDS — the exact defeating
 *        edit; bash uses zoom_sink.monitor while the resolver sees
 *        mic_silence_sink
 *        red: '[M126] MIC_SILENCE_SINK is assigned EXACTLY ONCE' (+1)
 *  M126b a harmless-looking DUPLICATE assignment with no alias at all — the
 *        precondition is broken either way, and the guard says so
 *        red: '[M126] …' (+2)
 *  M126c the same route via BOT_MIC_SOURCE
 *        red: '[M126] BOT_MIC_SOURCE is assigned EXACTLY ONCE'
 *
 * ---- QA hole 6: the SHIPPED defaults were unpinned ----
 *
 * stepZoomMuteWatcher is tested exhaustively with INJECTED configs, so every
 * anti-oscillator property was proven for configs that never ship. The default
 * `{ confirmations: 2, cooldownMs: 30_000 }` -> `{ 1, 0 }` survived the WHOLE
 * suite while restoring the toggle oscillator the machine exists to prevent.
 * And ZOOM_RETIREMENT_REQUIRED_STRIKES was pinned while its justification —
 * "each strike costs a full watcher cycle (~15s poll + cooldown)" — rested on
 * the UNPINNED cooldown: a pinned constant leaning on an unpinned one.
 *
 * Now hoisted to `zoomMuteWatcherDefaultConfig` and pinned BEHAVIOURALLY: the
 * state machine is stepped WITH THE SHIPPED CONFIG to prove a single reading
 * does not click and a second click inside the cooldown is refused, plus the
 * relationship the strike count depends on.
 *
 *  M127  the shipped config -> { confirmations: 1, cooldownMs: 0 }
 *        red: '[M127] SHIPPED: a single unmuted reading does not click' (+8)
 *  M127b the shipped cooldown drops to 500ms, undermining the 2-strike premise
 *        red: '[M127] and the cooldown is large enough that a strike really
 *        does cost a cycle' (+1)
 *  M128  the shipped poll budget drops below the old 750ms single sample
 *        red: '[M128] the budget outlasts the old 750ms single sample' (+9)
 *
 * ---- M124 REMOVED rather than narrowed ----
 *
 * It asserted `both lookups precede the first return` plus the exact text
 * `if (unmutedIcon && mutedIcon) {` — a spelling lock on a property M122 proves
 * behaviourally, which would redden on a cosmetic rename with no behaviour
 * change. Same class as the old M58 guard that codified a defect. The removal
 * is VERIFIED, not assumed: reintroducing an early `return` before the second
 * lookup reddens '[M122] two contradicting VISIBLE glyphs resolve to muted'
 * (+4). QA flagged its own guard; it was right to.
 *
 * ---- Recorded as KNOWN DEBT, deliberately not fixed ----
 *
 *  MAX_PATH_DEPTH (12) is a LIVE property, not only a mutant: the path is not
 *  necessarily rooted at `body` for a real Zoom footer button, so two elements
 *  can share a key and retiring one control could retire an unrelated one.
 *  Bounded by the two-strike corroboration and the all-retired reset, so a
 *  collision costs extra cycles rather than a permanently blind watcher. Noted
 *  where the constant is defined. QA holds 3/4/5/7/8/9/10 are coverage gaps
 *  with correct shipped behaviour and are deliberately NOT addressed here.
 *
 * ---- The two live-run misdirections, 2026-09-02 ----
 *
 * A — the mode=off note was FACTUALLY WRONG and this file contradicted itself.
 * It told the operator "the peer registry is not installed, so audioSenders
 * reads unreadable". Both claims are false: the RTCPeerConnection ctor patch
 * sits ABOVE the KILL SWITCH BOUNDARY and is deliberately NOT gated (that
 * comment says so, 800 lines away, and `registryPresent` is set before the
 * off-mode early return). Mode off IS the first live run, and
 * audioSenders/sweepDisabled is the only page-side evidence that Zoom ATTACHED
 * the mic to a sender rather than merely being offered a device — so the note
 * instructed the operator to discard the informative signal for the weaker one,
 * inside the same sentence as a true statement about tracksLocked.
 *
 *  M129  the false note restored   red: '[M129] the off-note must NEVER claim
 *        the peer registry is not installed' (+5) — a NEGATIVE guard, because a
 *        wrong operator note rots back in silently
 *  M129b the note drops WHY those counters matter (attachment vs availability)
 *        red: '[M129] and says WHY they matter'
 *
 * B — start.sh conflated "source absent" with "source unmuted", and the two
 * branches were INVERTED. The predicate returned false for BOTH states and was
 * reused as the post-unload verdict, so three materially different states
 * printed one string — and present-and-MUTED (safe) was reported as able to
 * transmit while present-and-UNMUTED (the one dangerous state) got the
 * reassuring "capture source removed" line. Replaced by `zoom_mic_state`, a
 * 4-state classifier (absent | muted | unmuted | unknown) with three distinct
 * post-unload messages; "capable of transmitting" now appears exactly once in
 * the executed script, on the DANGER branch only. A never-created source gets
 * its own prominent MIC FIX NOT IN FORCE line, stated as SAFE — it is the
 * likeliest way this whole change silently fails, and it was being reported as
 * a privacy incident.
 *
 *  M130  the dangerous post-unload state prints the SAFE message  red: (+2)
 *  M130b a never-created source reported as a privacy incident    red: (+6)
 *  M130c/M130f a `case` arm renamed so a state falls through to `*)` —
 *        SURVIVED at first: every assertion checked that a string EXISTS, not
 *        that its arm DISPATCHES, so MIC FIX NOT IN FORCE became unreachable
 *        while staying "present". Fixed by asserting the dispatch arms.
 *        red: '[M130] an absent) arm that actually matches the classifier output'
 *  M130d 'unknown' folded into 'unmuted' — SURVIVED at first for the same
 *        reason (`echo unknown` still appeared elsewhere in the function)
 *        red: '[M130] an undeterminable mute state falls to an explicit
 *        unknown, never to a verdict' (+1)
 *  M130e/M130g 'absent' reported as another state                 red: (+1)
 *
 *  M131  START.SH IS SHELL, BUT THAT WAS NOT THE CEILING. `zoom_mic_state`
 *        depends only on `pactl`, so it is EXTRACTED FROM start.sh AND EXECUTED
 *        against a fake pactl on PATH — behavioural proof, not a source guard,
 *        covering all four states plus the older-pactl list-sources fallback and
 *        the "don't read an earlier source's Mute line" case. It fails loudly if
 *        bash/awk are unavailable rather than skipping.
 *
 *        NOTE ON A VACUOUS PASS CAUGHT HERE: the first version passed fixtures
 *        through JSON.stringify into `printf '%s'`, which ESCAPED THE TABS, so
 *        awk saw one field and every scenario returned `absent` — and the
 *        `absent` assertion PASSED FOR THE WRONG REASON (control accidentally
 *        equal to the ambient value). Only the other five failing exposed it.
 *        The fake now answers from FILES, and the `muted` case is labelled as
 *        the fixture check because it is reachable only if the tab-separated
 *        listing actually parsed.
 *
 * Last run: 53 earlier + 24 observability/verification + 22 discovery +
 *           20 root-cause/hands-off + 9 re-read pinning + 34 critical-2/warnings
 *           + 9 reference-vs-referent/hole-2 + 7 position-blindness/shipped-defaults
 *           + 10 live-run misdirections = 188 attempted, 188 caught
 *           (after fixing the six real gaps the survivors exposed: M120, M121,
 *           M86d/M125 the ALIASED echo loop, M118, M122 the tier order — plus
 *           M82's ordering-only proof and M104b's hang),
 *           0 stale anchors. Five edits SURVIVED on first attempt: four were
 *           no-op or wrongly-placed edits (M79, M87b, M88b v1, and M83 v1's
 *           weakness aside) and were re-run as real edits; M78 turned out not to
 *           be a real invariant at all and is recorded as such rather than
 *           given a manufactured mutation. An edit that does not change
 *           behaviour is not evidence of anything.
 *           The two v1 edits noted above are counted once each, in their
 *           caught (v2) form; both v1 forms are recorded because an edit that
 *           does not actually change behaviour is not evidence of anything.
 *
 * NOTE on source guards (M30/M30b, M36, M52*, M58*): startZoomMuteWatcher and
 * startZoomOutboundAudioGuard are setInterval + Playwright bodies and CANNOT be
 * executed by a unit test here. Those ids are guarded by SOURCE assertions on
 * the exact statement text. That is weaker than a behavioural test and is not
 * presented as equivalent — it pins the wiring, never the runtime behaviour.
 * Everything the two drivers decide has been extracted into pure functions
 * (reportZoomAudioGuardTick, reportZoomMuteClick, stepZoomMuteWatcher) which ARE
 * tested behaviourally; only the plumbing between them rests on source guards.
 */

import {
  readZoomMicState,
  selectZoomMicToggle,
  describeZoomMicCandidates,
  normaliseZoomMicText,
  silenceOutboundAudioTracks,
  describeOutboundAudioSweep,
  ZoomMicProbe,
  ZoomMicCandidate,
  installOutboundAudioLockInPage,
  describeOutboundAudioLock,
  parseZoomAudioLockEnv,
  stepZoomMuteWatcher,
  zoomMuteWatcherInitialState,
  makeZoomMicProbe,
  parseZoomAudioLockMode,
  readZoomMicCandidateByKey,
  zoomMuteWatcherDefaultConfig,
  ZOOM_RETIREMENT_REQUIRED_STRIKES,
  describeZoomSendSide,
  resetZoomRetirement,
  zoomRetirementIsAbsorbing,
  zoomRetirementInitialState,
  stepZoomRetirement,
  zoomMutePollDefaults,
  pollZoomMuteSettled,
  readZoomMuteVocabulary,
  reportZoomAudioGuardTick,
  reportZoomMuteClick,
  OutboundAudioLockResult,
  OutboundAudioSweepResult,
  ZoomMicSelection,
  isZoomAudioOptionsControl,
} from './prepare';
import {
  zoomMicToggleSelectors,
  zoomNonMicLabelSubstrings,
  zoomAudioOptionsExactLabels,
  zoomMicUnmutedClassHints,
  zoomMicIconUnmutedClasses,
  zoomMicIconMutedClasses,
} from './selectors';
import { readFileSync, mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { execFileSync } from 'node:child_process';

let passed = 0;
let failed = 0;

function assertEqual(actual: unknown, expected: unknown, label: string): void {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (ok) {
    passed++;
    console.log(`  ✅ ${label}`);
  } else {
    failed++;
    console.log(`  ❌ ${label}`);
    console.log(`     expected: ${JSON.stringify(expected)}`);
    console.log(`     actual:   ${JSON.stringify(actual)}`);
  }
}

/**
 * Build a probe; every field defaults to "absent" so each test varies exactly
 * one thing. Delegates to the PRODUCTION factory rather than keeping a parallel
 * copy: a hand-maintained duplicate silently stops covering fields added later,
 * which is how a probe field can ship with no test at all.
 */
const probe = (p: Partial<ZoomMicProbe> = {}): ZoomMicProbe => makeZoomMicProbe(p);

const candidate = (selector: string, index: number, p: Partial<ZoomMicProbe>): ZoomMicCandidate => ({
  selector,
  index,
  probe: probe(p),
});

console.log('\n=== LAYER 1: readZoomMicState — the live regression fixture ===');

// 1. THE BUG. Live DOM 2026-09-01: button.join-audio-container__btn had
//    aria-label="audio". Exact equality against 'Mute'/'Unmute' matched nothing.
{
  const r = readZoomMicState(probe({ ariaLabel: 'audio', className: 'footer-button-base__button ax-outline join-audio-container__btn' }));
  assertEqual(r.kind, 'not-mute-toggle', 'LIVE FIXTURE aria-label="audio" reads not-mute-toggle (never "unmuted")');
}

// 2. And critically: it must NOT be selected, so the bot does not click a
//    control it has not identified (clicking blind can UNMUTE it).
{
  const sel = selectZoomMicToggle([candidate('button.join-audio-container__btn', 0, { ariaLabel: 'audio' })]);
  assertEqual(sel, null, 'LIVE FIXTURE aria-label="audio" is not selected as a toggle');
}

console.log('\n=== LAYER 1: mute/unmute vocabulary + the substring trap ===');

// 3. "Unmute" is the OFFERED ACTION -> currently muted. Note "Unmute" contains
//    "mute": a naive substring test in the wrong order inverts this.
assertEqual(readZoomMicState(probe({ ariaLabel: 'Unmute' })).kind, 'muted', '"Unmute" => muted (substring order: unmute tested before mute)');
assertEqual(readZoomMicState(probe({ ariaLabel: 'Mute' })).kind, 'unmuted', '"Mute" => unmuted');

// 4. Vocabulary variants Zoom has shipped / may ship.
assertEqual(readZoomMicState(probe({ ariaLabel: 'unmute my microphone' })).kind, 'muted', '"unmute my microphone" => muted');
assertEqual(readZoomMicState(probe({ ariaLabel: 'mute my microphone' })).kind, 'unmuted', '"mute my microphone" => unmuted');
assertEqual(readZoomMicState(probe({ ariaLabel: '  UNMUTE  ' })).kind, 'muted', 'case + whitespace tolerant: "  UNMUTE  " => muted');
assertEqual(readZoomMicState(probe({ ariaLabel: 'Mute\n  (Alt+A)' })).kind, 'unmuted', 'collapses newlines: "Mute (Alt+A)" => unmuted');

// 5. Non-state audio vocabulary.
assertEqual(readZoomMicState(probe({ ariaLabel: 'Join Audio' })).kind, 'not-mute-toggle', '"Join Audio" => not-mute-toggle');
assertEqual(readZoomMicState(probe({ ariaLabel: 'Audio Settings' })).kind, 'not-mute-toggle', '"Audio Settings" => not-mute-toggle');

// 6. 'audio' is an EXACT-match-only non-toggle word — as a SUBSTRING it must not
//    override real mute vocabulary.
assertEqual(readZoomMicState(probe({ ariaLabel: 'unmute my audio' })).kind, 'muted', '"unmute my audio" => muted (the word "audio" does not win)');

// 7. No label at all, no other evidence.
assertEqual(readZoomMicState(probe({})).kind, 'unknown', 'no attributes at all => unknown');
assertEqual(readZoomMicState(probe({ ariaLabel: 'Reactions' })).kind, 'not-mute-toggle', 'unrelated label => not-mute-toggle');

console.log('\n=== LAYER 1: class-hint fallback (unmuted-before-muted trap) ===');

// 8. Every unmuted hint contains the substring 'muted' ('svgaudiounmuted', and
//    the bare-word 'unmuted' fallback). Checking the muted list first inverts
//    the reading and the bot never mutes itself. Load-bearing per mutation M3.
//
//    UPDATED 2026-09-02 (W11): the DIRECTION is still what this pins, but the
//    tier no longer produces the actionable `unmuted` — it produces
//    `unmuted-unconfirmed`. M3 is still killed: swapping the two hint lists
//    makes this read `muted`, which is a different kind, so this assertion goes
//    red exactly as before.
assertEqual(
  readZoomMicState(probe({ ariaLabel: null, descendantClassNames: ['zm-icon SvgAudioUnmuted'] })).kind,
  'unmuted-unconfirmed',
  'icon class "SvgAudioUnmuted" => unmuted-unconfirmed (NOT muted — it contains "muted")',
);
assertEqual(
  readZoomMicState(probe({ ariaLabel: null, descendantClassNames: ['zm-icon SvgAudioMuted'] })).kind,
  'muted',
  'icon class "SvgAudioMuted" => muted',
);
assertEqual(
  readZoomMicState(probe({ className: 'btn is-muted' })).kind,
  'muted',
  'button class "is-muted" => muted',
);
// 8b. The bare-word fallback: an unseen future Zoom class name still resolves —
//     to a DIRECTION that is reported, not to a click.
assertEqual(
  readZoomMicState(probe({ descendantClassNames: ['zm-btn__icon--unmuted-state'] })).kind,
  'unmuted-unconfirmed',
  'unseen class "...--unmuted-state" => unmuted-unconfirmed via the bare-word fallback',
);
assertEqual(
  readZoomMicState(probe({ descendantClassNames: ['zm-btn__icon--muted-state'] })).kind,
  'muted',
  'unseen class "...--muted-state" => muted via the bare-word fallback',
);
// 9. A label with mute vocabulary must OUTRANK a contradicting class hint.
assertEqual(
  readZoomMicState(probe({ ariaLabel: 'Unmute', descendantClassNames: ['SvgAudioUnmuted'] })).kind,
  'muted',
  'aria-label outranks a contradicting class hint',
);

console.log('\n=== LAYER 1: precedence — the live "audio" element is now READABLE ===');

// 7b. THE POINT OF THE REORDER. The old code classified aria-label="audio" as
//     non-state and returned IMMEDIATELY, so aria-pressed and the class hints
//     were never consulted. An uninformative LABEL is not an uninformative
//     ELEMENT. These four cases are the live DOM shape plus one extra signal.
assertEqual(
  readZoomMicState(probe({ ariaLabel: 'audio', ariaPressed: 'true' })).kind,
  'muted',
  'LIVE SHAPE aria-label="audio" + aria-pressed="true" => muted (was: written off as non-state)',
);
assertEqual(
  readZoomMicState(probe({ ariaLabel: 'audio', descendantClassNames: ['SvgAudioMuted'] })).kind,
  'muted',
  'LIVE SHAPE aria-label="audio" + muted icon class => muted',
);
// UPDATED 2026-09-02 (W11). This case is EXACTLY the live failure, and it is no
// longer clickable: a HIDDEN-inclusive class-hint substring is the weakest
// evidence there is and it produced this reading against a bot that was really
// muted. The actionable version of the same shape needs the glyph to be
// RENDERED (the case immediately below).
assertEqual(
  readZoomMicState(probe({ ariaLabel: 'audio', descendantClassNames: ['SvgAudioUnmuted'] })).kind,
  'unmuted-unconfirmed',
  'LIVE SHAPE aria-label="audio" + unmuted class HINT => unmuted-unconfirmed (NOT clickable)',
);
assertEqual(
  readZoomMicState(probe({ ariaLabel: 'audio', visibleDescendantClassNames: ['SvgAudioUnmuted'] })).kind,
  'unmuted',
  'LIVE SHAPE aria-label="audio" + a RENDERED unmuted glyph => unmuted (clickable, on the precise tier)',
);
// ...but a weak/absent signal still must NOT make it clickable.
assertEqual(
  readZoomMicState(probe({ ariaLabel: 'audio', ariaPressed: 'false' })).kind,
  'not-mute-toggle',
  'aria-label="audio" + aria-pressed="false" => still not-mute-toggle (no click)',
);

// 7c. aria-pressed outranks a CONTRADICTING class hint (it is real ARIA state,
//     the class names are guesses). Locks the new precedence.
assertEqual(
  readZoomMicState(probe({ ariaPressed: 'true', descendantClassNames: ['SvgAudioUnmuted'] })).kind,
  'muted',
  'aria-pressed="true" outranks a contradicting unmuted class hint',
);
// 7d. ...but mute VOCABULARY still outranks aria-pressed: it needs no polarity
//     assumption, so it stays the strongest signal.
assertEqual(
  readZoomMicState(probe({ ariaLabel: 'Mute', ariaPressed: 'true' })).kind,
  'unmuted',
  'aria-label "Mute" outranks aria-pressed="true" (label needs no polarity assumption)',
);

console.log('\n=== LAYER 1: aria-pressed is deliberately asymmetric ===');

// 10. Polarity of aria-pressed on Zoom's toggle is unconfirmed. A wrong
//     "unmuted" read causes a click that UNMUTES the bot; a wrong "muted" read
//     only leaves layer 2 in charge. So pressed=true may conclude muted, but
//     pressed=false must NOT conclude unmuted.
assertEqual(readZoomMicState(probe({ ariaPressed: 'true' })).kind, 'muted', 'aria-pressed="true" => muted');
assertEqual(readZoomMicState(probe({ ariaPressed: 'false' })).kind, 'unknown', 'aria-pressed="false" => unknown, NEVER unmuted (would trigger a click)');

console.log('\n=== LAYER 1: selectZoomMicToggle picks the first CONFIDENT candidate ===');

// 11. The real shape of the live failure: several matches, the first unreadable.
{
  const sel = selectZoomMicToggle([
    candidate('footer button[aria-label*="audio" i]', 0, { ariaLabel: 'audio' }),
    candidate('button[aria-label*="mute" i]', 2, { ariaLabel: 'Mute' }),
  ]);
  assertEqual(sel?.candidate.selector, 'button[aria-label*="mute" i]', 'skips the unreadable candidate and selects the readable one');
  assertEqual(sel?.candidate.index, 2, 'preserves nth index so the same element is clicked');
  assertEqual(sel?.reading.kind, 'unmuted', 'reports the reading that justified selection');
}
assertEqual(selectZoomMicToggle([]), null, 'no candidates => null');
{
  const sel = selectZoomMicToggle([candidate('a', 0, { ariaLabel: 'audio' }), candidate('b', 0, {})]);
  assertEqual(sel, null, 'all candidates unreadable => null (no click)');
}
// 12. Diagnostic digest must name every candidate — the missing diagnostic in the live run.
{
  const d = describeZoomMicCandidates([
    candidate('sel-a', 0, { ariaLabel: 'audio' }),
    candidate('sel-b', 1, { ariaLabel: 'Mute' }),
  ]);
  assertEqual(d.includes('sel-a[0]') && d.includes('audio') && d.includes('not-mute-toggle'), true, 'digest names the unreadable candidate and its reading');
  assertEqual(d.includes('sel-b[1]') && d.includes('unmuted'), true, 'digest names the readable candidate and its reading');
  assertEqual(d.includes('aria-pressed=absent'), true, 'digest reports aria-pressed (absent) — the fact a live run must settle');
  assertEqual(
    describeZoomMicCandidates([candidate('s', 0, { ariaLabel: 'audio', ariaPressed: 'true' })]).includes('aria-pressed=true'),
    true,
    'digest reports aria-pressed when present',
  );
  assertEqual(describeZoomMicCandidates([]), 'no mic-control candidates matched any selector', 'digest reports the empty case explicitly');
}

assertEqual(normaliseZoomMicText(null), '', 'normalise(null) => ""');
assertEqual(normaliseZoomMicText('  A   B '), 'a b', 'normalise lowercases and collapses whitespace');

console.log('\n=== W3: other buttons that merely CONTAIN mute vocabulary ===');

// 42. [M26] "Ask to unmute" contains "unmute". Before the fix it read as MUTED,
//     so ensureZoomMutedInMeeting returned {muted:true} and logged "Mic already
//     muted" WITHOUT EVER CLICKING — a silent failure in the safe-looking
//     direction, with the layer-1 WARNING never firing.
assertEqual(
  readZoomMicState(probe({ ariaLabel: 'Ask to unmute' })).kind,
  'not-mic-control',
  '[M26] "Ask to unmute" => not-mic-control (was: read as muted => silent false success)',
);
// 43. [M26] "Mute All" contains "mute". Before the fix it read as UNMUTED, so the
//     watcher would CLICK it — muting every other participant in the meeting.
assertEqual(
  readZoomMicState(probe({ ariaLabel: 'Mute All' })).kind,
  'not-mic-control',
  '[M26] "Mute All" => not-mic-control (was: read as unmuted => watcher would mute EVERYONE)',
);
for (const label of ['Unmute All', 'Ask all to unmute', 'Mute everyone', 'Unmute Participant 3']) {
  assertEqual(readZoomMicState(probe({ ariaLabel: label })).kind, 'not-mic-control', `"${label}" => not-mic-control`);
}
// 44. [M27] The rejection must return IMMEDIATELY — not fall through to the class
//     hints. A "Mute All" button can carry its own muted-looking icon class.
assertEqual(
  readZoomMicState(probe({ ariaLabel: 'Mute All', descendantClassNames: ['SvgAudioMuted'] })).kind,
  'not-mic-control',
  '[M27] a non-mic label is NOT rescued by a muted class hint',
);
assertEqual(
  readZoomMicState(probe({ ariaLabel: 'Mute All', ariaPressed: 'true' })).kind,
  'not-mic-control',
  '[M27] a non-mic label is NOT rescued by aria-pressed either',
);
// 45. A non-mic control is never selectable, so it can never be clicked.
assertEqual(
  selectZoomMicToggle([candidate('footer button[aria-label*="unmute" i]', 0, { ariaLabel: 'Ask to unmute' })]),
  null,
  'a non-mic control alone => no selection, no click',
);

// 46. [M26] THE REQUIRED FIXTURE: a page-wide "Ask to unmute" present AND the real
//     join-audio-container__btn behind it. The non-mic button is deliberately
//     placed FIRST — the worst case — and the REAL control must still win.
{
  const sel = selectZoomMicToggle([
    candidate('footer button[aria-label*="unmute" i]', 0, { ariaLabel: 'Ask to unmute' }),
    candidate('footer button[aria-label*="mute" i]', 1, { ariaLabel: 'Mute All' }),
    candidate('button.join-audio-container__btn', 0, { ariaLabel: 'Mute' }), // the real mic control
  ]);
  assertEqual(sel?.candidate.selector, 'button.join-audio-container__btn', '[M26] the REAL mic control wins over "Ask to unmute" / "Mute All"');
  assertEqual(sel?.reading.kind, 'unmuted', '[M26] and it is read as unmuted, so it WILL be clicked');
}

// 47. The live-observed shape survives the new guard: aria-label="audio" on the
//     real control is still classified as before (not swallowed by the new list).
assertEqual(readZoomMicState(probe({ ariaLabel: 'audio' })).kind, 'not-mute-toggle', 'the live "audio" label is unaffected by the non-mic list');
// 48. And a genuine mic label containing "unmute" is NOT caught by the new list.
assertEqual(readZoomMicState(probe({ ariaLabel: 'unmute my microphone' })).kind, 'muted', 'a real mic label is not mistaken for a non-mic control');

console.log('\n=== W3: selector-list shape guards ===');

// 49. [M28] No comma lists: a candidate is re-addressed as .nth(index) after being
//     probed with querySelectorAll, so a comma list would make that index depend
//     on two engines ordering a union identically.
assertEqual(
  zoomMicToggleSelectors.filter((sel) => sel.includes(',')),
  [],
  '[M28] no selector contains a comma list',
);
// 50. [M28] No page-wide mute-vocabulary selector: every entry carrying mute
//     vocabulary must also be scoped to the footer/toolbar, or "Ask to unmute"
//     anywhere on the page becomes a candidate again.
{
  const pageWideMuteVocab = zoomMicToggleSelectors.filter(
    (sel) => /mute/i.test(sel) && !/footer/i.test(sel) && !/join-audio-container/i.test(sel),
  );
  assertEqual(pageWideMuteVocab, [], '[M28] every mute-vocabulary selector is scoped to the footer/toolbar');
}
assertEqual(zoomMicToggleSelectors[0], 'button.join-audio-container__btn', 'the live-confirmed control is probed FIRST');
assertEqual(zoomNonMicLabelSubstrings.length >= 8, true, 'the non-mic vocabulary list is populated');

console.log('\n=== LAYER 2: silenceOutboundAudioTracks ===');

type FakeTrack = { kind: string; enabled: boolean };
type FakePeer = {
  connectionState?: string;
  getSenders?: () => { track: FakeTrack | null }[];
  getReceivers?: () => { track: FakeTrack }[];
};

function withRegistry<T>(registry: unknown, fn: () => T): T {
  const g = globalThis as unknown as { __vexa_peer_connections?: unknown };
  const had = '__vexa_peer_connections' in g;
  const prev = g.__vexa_peer_connections;
  if (registry === undefined) delete g.__vexa_peer_connections;
  else g.__vexa_peer_connections = registry;
  try {
    return fn();
  } finally {
    if (had) g.__vexa_peer_connections = prev;
    else delete g.__vexa_peer_connections;
  }
}

// 13. Registry absent (video-block init script not injected) — must be REPORTED,
//     not silently treated as success.
{
  const r = withRegistry(undefined, () => silenceOutboundAudioTracks(false));
  assertEqual([r.registryPresent, r.tracksDisabled], [false, 0], 'absent registry => registryPresent false, nothing disabled');
  assertEqual(describeOutboundAudioSweep(r).includes('registry absent'), true, 'absent registry is described as such');
}

// 14. The core guarantee: an enabled outbound audio track gets disabled.
{
  const mic: FakeTrack = { kind: 'audio', enabled: true };
  const cam: FakeTrack = { kind: 'video', enabled: true };
  const peer: FakePeer = { connectionState: 'connected', getSenders: () => [{ track: mic }, { track: cam }, { track: null }] };
  const r = withRegistry([peer], () => silenceOutboundAudioTracks(false));
  assertEqual(mic.enabled, false, 'GUARANTEE: outbound AUDIO track is disabled');
  assertEqual(cam.enabled, true, 'outbound VIDEO track is left alone (video is handled elsewhere)');
  assertEqual(
    [r.registryPresent, r.peerConnections, r.audioSendersFound, r.tracksDisabled, r.alreadyDisabled, r.errors],
    [true, 1, 1, 1, 0, 0],
    'counters report exactly one audio sender disabled',
  );
}

// 15. Incoming audio is what the transcription pipeline depends on. If someone
//     ever swaps getSenders for getReceivers, this goes red.
{
  const remote: FakeTrack = { kind: 'audio', enabled: true };
  const peer: FakePeer = { connectionState: 'connected', getSenders: () => [], getReceivers: () => [{ track: remote }] };
  withRegistry([peer], () => silenceOutboundAudioTracks(false));
  assertEqual(remote.enabled, true, 'INCOMING audio track is never touched (recording must keep working)');
}

// 16. voiceAgentEnabled: a voice agent legitimately transmits TTS.
{
  const mic: FakeTrack = { kind: 'audio', enabled: true };
  const peer: FakePeer = { connectionState: 'connected', getSenders: () => [{ track: mic }] };
  const r = withRegistry([peer], () => silenceOutboundAudioTracks(true));
  assertEqual(mic.enabled, true, 'voiceAgentEnabled => outbound audio stays ENABLED');
  assertEqual([r.skippedVoiceAgent, r.audioSendersFound], [true, 0], 'voiceAgentEnabled => skipped before touching the registry');
  assertEqual(describeOutboundAudioSweep(r).includes('voice agent'), true, 'voice-agent skip is described as such');
}

// 17. Idempotent re-sweep: an already-disabled track is counted separately, not
//     re-reported as a fresh disable (the guard logs only real disables).
{
  const mic: FakeTrack = { kind: 'audio', enabled: false };
  const peer: FakePeer = { connectionState: 'connected', getSenders: () => [{ track: mic }] };
  const r = withRegistry([peer], () => silenceOutboundAudioTracks(false));
  assertEqual([r.tracksDisabled, r.alreadyDisabled], [0, 1], 'already-disabled track counts as alreadyDisabled, not tracksDisabled');
}

// 18. A closed peer is skipped entirely.
{
  const mic: FakeTrack = { kind: 'audio', enabled: true };
  const peer: FakePeer = { connectionState: 'closed', getSenders: () => [{ track: mic }] };
  const r = withRegistry([peer], () => silenceOutboundAudioTracks(false));
  assertEqual([r.peerConnections, r.audioSendersFound], [0, 0], 'closed peer connection is skipped');
}

// 19. One bad peer must never abort the sweep — the LAST peer still gets silenced.
{
  const mic: FakeTrack = { kind: 'audio', enabled: true };
  const thrower: FakePeer = { connectionState: 'connected', getSenders: () => { throw new Error('context destroyed'); } };
  const good: FakePeer = { connectionState: 'connected', getSenders: () => [{ track: mic }] };
  const r = withRegistry([thrower, {} as FakePeer, null, good], () => silenceOutboundAudioTracks(false));
  assertEqual(mic.enabled, false, 'a throwing peer does not stop later peers from being silenced');
  assertEqual([r.tracksDisabled, r.errors], [1, 3], 'errors counted (throw + no-getSenders + null) while the good peer still disabled');
}

// 20. A non-array registry must not throw.
{
  const r = withRegistry({ nope: true }, () => silenceOutboundAudioTracks(false));
  assertEqual([r.registryPresent, r.errors], [false, 0], 'non-array registry => treated as absent, no throw');
}

// 21. Multiple peers, multiple audio senders.
{
  const a: FakeTrack = { kind: 'audio', enabled: true };
  const b: FakeTrack = { kind: 'audio', enabled: true };
  const r = withRegistry(
    [
      { connectionState: 'connected', getSenders: () => [{ track: a }] } as FakePeer,
      { connectionState: 'connecting', getSenders: () => [{ track: b }] } as FakePeer,
    ],
    () => silenceOutboundAudioTracks(false),
  );
  assertEqual([a.enabled, b.enabled], [false, false], 'every peer’s outbound audio is silenced');
  assertEqual([r.peerConnections, r.tracksDisabled], [2, 2], 'counters aggregate across peers');
  assertEqual(describeOutboundAudioSweep(r), 'pcs=2 audioSenders=2 disabled=2 alreadyDisabled=0 errors=0', 'sweep description reports the real counters');
}

console.log('\n=== LAYER 2 (primary): the IRREVOCABLE lock ===');

/**
 * A fake MediaStreamTrack whose `enabled` is a REAL accessor over a backing
 * field, so `real()` reports whether audio is genuinely stopped — independently
 * of whatever the lock's own getter later claims. That separation is the whole
 * point: it is what makes "sealed but still transmitting" a detectable bug
 * rather than an invisible one.
 */
function makeTrack(kind: string): { track: { kind: string; enabled: boolean }; real: () => boolean } {
  let real = true;
  // `enabled` lives on the PROTOTYPE, as it does on a real MediaStreamTrack. That
  // matters: the lock shadows it with an OWN accessor, so deleting the seal must
  // reveal the prototype accessor again. The earlier fake put `enabled` directly
  // on the instance, where defineProperty REPLACED it outright — which is exactly
  // why the de-seal case (BLOCKER 1) went unnoticed by 138 passing tests.
  const proto = {
    get enabled(): boolean {
      return real;
    },
    set enabled(v: boolean) {
      real = v;
    },
  };
  const track = Object.create(proto) as { kind: string; enabled: boolean };
  track.kind = kind;
  return { track, real: () => real };
}

type FakeSenderProto = { replaceTrack?: unknown };
type FakePcProto = { addTrack?: unknown };

/** Install fake globals, always starting from a clean lock state, then restore. */
function withGlobals(patch: Record<string, unknown>, fn: () => void): void {
  const g = globalThis as unknown as Record<string, unknown>;
  const saved = new Map<string, PropertyDescriptor | undefined>();
  for (const k of Object.keys(patch)) {
    saved.set(k, Object.getOwnPropertyDescriptor(g, k));
    Object.defineProperty(g, k, { value: patch[k], writable: true, configurable: true, enumerable: false });
  }
  const lockDesc = Object.getOwnPropertyDescriptor(g, '__vexa_zoom_audio_lock');
  delete g.__vexa_zoom_audio_lock;
  try {
    fn();
  } finally {
    delete g.__vexa_zoom_audio_lock;
    if (lockDesc) Object.defineProperty(g, '__vexa_zoom_audio_lock', lockDesc);
    for (const [k, d] of saved) {
      if (d) Object.defineProperty(g, k, d);
      else delete g[k];
    }
  }
}

/** A fake RTCPeerConnection CONSTRUCTOR (a function, as the real global is), so
 *  the constructor patch that builds the Zoom-local registry actually engages.
 *  A plain `{ prototype }` object does not — `typeof` must be 'function'. */
function makeFakePcCtor(proto: Record<string, unknown>): unknown {
  const Ctor = function (this: unknown) { /* no-op peer */ } as unknown as { prototype: Record<string, unknown> };
  Ctor.prototype = proto;
  return Ctor;
}

const peerWith = (tracks: { kind: string; enabled: boolean }[]) => ({
  connectionState: 'connected',
  getSenders: () => tracks.map((t) => ({ track: t })),
});

// 22. [M17] ORDERING: the real audio must be stopped BEFORE the seal goes on.
//     If defineProperty ran first, `track.enabled` would read false while the
//     underlying track kept transmitting — a lie, not a guarantee. real() is
//     the only witness that can tell those two apart.
{
  const mic = makeTrack('audio');
  withGlobals({ __vexa_peer_connections: [peerWith([mic.track])] }, () => {
    const r = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    assertEqual(mic.real(), false, '[M17] REAL audio state is stopped (enabled=false applied BEFORE sealing)');
    assertEqual(mic.track.enabled, false, 'sealed getter reports false');
    assertEqual([r.tracksLocked, r.registryPresent], [1, true], 'existing outbound audio track is locked');
  });
}

// 23. [M18] "CANNOT BE UNMUTED": an assignment of true must not take effect.
{
  const mic = makeTrack('audio');
  withGlobals({ __vexa_peer_connections: [peerWith([mic.track])] }, () => {
    installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    mic.track.enabled = true; // <- the unmute attempt
    assertEqual(mic.track.enabled, false, '[M18] GUARANTEE: setting enabled=true does NOT unmute (getter still false)');
    assertEqual(mic.real(), false, '[M18] GUARANTEE: real audio state still stopped after the unmute attempt');
    const again = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    assertEqual(again.blockedUnmutes, 1, '[M18] the refused unmute attempt is counted and reportable');
  });
}

// 24. Repeated unmute attempts are all refused and all counted.
{
  const mic = makeTrack('audio');
  withGlobals({ __vexa_peer_connections: [peerWith([mic.track])] }, () => {
    installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    mic.track.enabled = true;
    mic.track.enabled = true;
    mic.track.enabled = true;
    assertEqual(mic.real(), false, 'three unmute attempts all refused');
    assertEqual(installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true }).blockedUnmutes, 3, 'all three attempts counted');
  });
}

// 25. Setting enabled=false is NOT counted as a blocked unmute (it agrees with us).
{
  const mic = makeTrack('audio');
  withGlobals({ __vexa_peer_connections: [peerWith([mic.track])] }, () => {
    installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    mic.track.enabled = false;
    assertEqual(installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true }).blockedUnmutes, 0, 'enabled=false is not a blocked unmute');
  });
}

// 26. Outbound VIDEO is never sealed — the voice-agent canvas path injects video
//     tracks via addTrack and must keep working.
{
  const cam = makeTrack('video');
  withGlobals({ __vexa_peer_connections: [peerWith([cam.track])] }, () => {
    const r = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    assertEqual(cam.real(), true, 'outbound VIDEO track is left enabled and unsealed');
    assertEqual(r.tracksLocked, 0, 'no video track counted as locked');
    cam.track.enabled = true;
    assertEqual(cam.real(), true, 'video track remains settable');
  });
}

// 27. [M19] addTrack: a NEWLY ADDED outbound audio track is silenced AT BIRTH —
//     no polling interval has elapsed, and it is disabled BEFORE the original
//     addTrack runs, so there is no transmit window at all.
{
  const fresh = makeTrack('audio');
  const seen: { enabledAtAttach: boolean }[] = [];
  const pcProto: FakePcProto = {
    addTrack(track: { enabled: boolean }) {
      seen.push({ enabledAtAttach: track.enabled });
      return { track };
    },
  };
  withGlobals({ RTCPeerConnection: { prototype: pcProto }, __vexa_peer_connections: [] }, () => {
    const r = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    assertEqual(r.patchedAddTrack, true, '[M19] addTrack was patched');
    (pcProto.addTrack as (t: unknown) => unknown)(fresh.track);
    assertEqual(fresh.real(), false, '[M19] ZERO-WINDOW: track added via addTrack is disabled with no interval elapsed');
    assertEqual(seen[0].enabledAtAttach, false, '[M19] it was already disabled BEFORE the original addTrack ran');
    fresh.track.enabled = true;
    assertEqual(fresh.real(), false, '[M19] the newly added track is also sealed against unmuting');
  });
}

// 28. [M20] replaceTrack: a REPLACED mic track is silenced at birth. This is the
//     hole a 10s poll cannot close — Zoom does replace the mic track.
{
  const replacement = makeTrack('audio');
  let receivedEnabled: boolean | null = null;
  const senderProto: FakeSenderProto = {
    replaceTrack(track: { enabled: boolean }) {
      receivedEnabled = track.enabled;
      return Promise.resolve();
    },
  };
  withGlobals({ RTCRtpSender: { prototype: senderProto }, __vexa_peer_connections: [] }, () => {
    const r = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    assertEqual(r.patchedReplaceTrack, true, '[M20] replaceTrack was patched');
    void (senderProto.replaceTrack as (t: unknown) => unknown)(replacement.track);
    assertEqual(replacement.real(), false, '[M20] ZERO-WINDOW: replacement mic track is disabled with no interval elapsed');
    assertEqual(receivedEnabled, false, '[M20] it was already disabled BEFORE the original replaceTrack ran');
    replacement.track.enabled = true;
    assertEqual(replacement.real(), false, '[M20] the replacement track is also sealed');
  });
}

// 29. The patches are idempotent — re-installing must not double-wrap (which
//     would make every counter double-count and stack wrappers all call long).
{
  const pcProto: FakePcProto = { addTrack: (t: unknown) => t };
  withGlobals({ RTCPeerConnection: { prototype: pcProto }, __vexa_peer_connections: [] }, () => {
    const first = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    const wrapperAfterFirst = pcProto.addTrack;
    const second = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    assertEqual([first.alreadyInstalled, second.alreadyInstalled], [false, true], 're-install reports alreadyInstalled');
    assertEqual(second.patchedAddTrack, true, 'patch state persists across re-install');
    assertEqual(pcProto.addTrack === wrapperAfterFirst, true, 'addTrack was NOT wrapped a second time');
  });
}

// 30. An already-locked track is not re-locked (WeakSet identity), so counters
//     stay meaningful across the periodic re-install.
{
  const mic = makeTrack('audio');
  withGlobals({ __vexa_peer_connections: [peerWith([mic.track])] }, () => {
    assertEqual(installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true }).tracksLocked, 1, 'first install locks the track');
    assertEqual(installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true }).tracksLocked, 1, 'second install does not re-count it');
  });
}

// 31. [M21] voiceAgentEnabled bypasses EVERY part of the lock — no seal, no
//     prototype patching. A voice agent must be able to transmit TTS.
{
  const mic = makeTrack('audio');
  const pcProto: FakePcProto = { addTrack: (t: unknown) => t };
  const original = pcProto.addTrack;
  withGlobals({ RTCPeerConnection: { prototype: pcProto }, __vexa_peer_connections: [peerWith([mic.track])] }, () => {
    const r = installOutboundAudioLockInPage({ voiceAgentEnabled: true, sealEnabled: true });
    assertEqual(mic.real(), true, '[M21] voice agent: outbound audio stays ENABLED');
    assertEqual(pcProto.addTrack === original, true, '[M21] voice agent: addTrack is NOT patched');
    assertEqual(
      [r.skippedVoiceAgent, r.tracksLocked, r.patchedAddTrack],
      [true, 0, false],
      '[M21] voice agent: nothing locked, nothing patched',
    );
    mic.track.enabled = true;
    assertEqual(mic.real(), true, '[M21] voice agent: the track remains settable (can unmute)');
  });
}

// 32. Inbound audio is never touched, even by the lock.
{
  const remote = makeTrack('audio');
  const peer = { connectionState: 'connected', getSenders: () => [], getReceivers: () => [{ track: remote.track }] };
  withGlobals({ __vexa_peer_connections: [peer] }, () => {
    installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    assertEqual(remote.real(), true, 'INCOMING audio track is never sealed (transcript must keep working)');
    remote.track.enabled = false;
    remote.track.enabled = true;
    assertEqual(remote.real(), true, 'incoming track stays fully settable');
  });
}

// 33. Missing APIs must not throw — the lock has to degrade, not crash.
{
  withGlobals({ __vexa_peer_connections: [] }, () => {
    const r = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    assertEqual(
      [r.patchedAddTrack, r.patchedReplaceTrack, r.errors],
      [false, false, 0],
      'absent RTCPeerConnection / RTCRtpSender => no patches, no errors',
    );
  });
}

// 34. describeOutboundAudioLock reports what actually happened.
{
  const pcProto: FakePcProto = { addTrack: (t: unknown) => t };
  withGlobals({ RTCPeerConnection: makeFakePcCtor(pcProto as Record<string, unknown>) }, () => {
    const r = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    assertEqual(
      describeOutboundAudioLock(r),
      'tracksLocked=0 verified=0 resealed=0 blockedUnmutes=0 patched=[ctor,addTrack] registry=ABSENT errors=0',
      'lock description reports the patches applied; registry is ABSENT on the FIRST pass because it is read before the ctor patch creates it',
    );
    // ...and PRESENT on the next pass, once the constructor patch has created it.
    assertEqual(
      describeOutboundAudioLock(installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true })).includes('registry=present'),
      true,
      'the Zoom-local registry exists from the second pass onward',
    );
  });
  assertEqual(
    describeOutboundAudioLock({
      sealEnabled: true, skippedVoiceAgent: true, alreadyInstalled: false, registryPresent: false, tracksLocked: 0,
      blockedUnmutes: 0, tracksVerified: 0, tracksResealed: 0, patchedConstructor: false,
      patchedAddTrack: false, patchedAddTransceiver: false, patchedReplaceTrack: false,
      patchedGetUserMedia: false, errors: 0,
    }).includes('UNLOCKED'),
    true,
    'voice-agent lock description says UNLOCKED',
  );
}

console.log('\n=== QA nit: the fallback SWEEP must read both registries ===');

// 70. [M42] THE NIT. silenceOutboundAudioTracks read only Meet's registry, which
//     is cameraEnabled-gated and absent by default — so the `errors > 0` fallback
//     for an unsealable track invoked a sweep that found no peers and disabled
//     nothing. Reproduces QA's case C: the ZOOM-LOCAL registry alone must work.
{
  const mic = makeTrack('audio');
  const g5 = globalThis as unknown as Record<string, unknown>;
  const savedZ = Object.getOwnPropertyDescriptor(g5, '__vexa_zoom_peer_connections');
  const savedM = Object.getOwnPropertyDescriptor(g5, '__vexa_peer_connections');
  g5.__vexa_zoom_peer_connections = [peerWith([mic.track])];
  delete g5.__vexa_peer_connections; // DEFAULT CONFIG: Meet's registry absent
  try {
    const r = silenceOutboundAudioTracks(false);
    assertEqual(r.registryPresent, true, '[M42] the ZOOM-LOCAL registry alone is enough for the sweep');
    assertEqual([r.audioSendersFound, r.tracksDisabled], [1, 1], '[M42] the track is found and disabled with Meet’s registry absent');
    assertEqual(mic.real(), false, '[M42] real audio is stopped by the sweep');
  } finally {
    delete g5.__vexa_zoom_peer_connections;
    if (savedZ) Object.defineProperty(g5, '__vexa_zoom_peer_connections', savedZ);
    if (savedM) Object.defineProperty(g5, '__vexa_peer_connections', savedM);
  }
}

// 71. [M42] A peer present in BOTH registries is swept ONCE, not twice — dedup by
//     identity, so the counters stay truthful when cameraEnabled is on.
{
  const mic = makeTrack('audio');
  const shared = peerWith([mic.track]);
  const g5 = globalThis as unknown as Record<string, unknown>;
  const savedZ = Object.getOwnPropertyDescriptor(g5, '__vexa_zoom_peer_connections');
  const savedM = Object.getOwnPropertyDescriptor(g5, '__vexa_peer_connections');
  g5.__vexa_zoom_peer_connections = [shared];
  g5.__vexa_peer_connections = [shared];
  try {
    const r = silenceOutboundAudioTracks(false);
    assertEqual(r.peerConnections, 1, '[M42] a peer in both registries is counted ONCE');
    assertEqual(r.audioSendersFound, 1, '[M42] and its sender is swept once, not twice');
  } finally {
    delete g5.__vexa_zoom_peer_connections;
    delete g5.__vexa_peer_connections;
    if (savedZ) Object.defineProperty(g5, '__vexa_zoom_peer_connections', savedZ);
    if (savedM) Object.defineProperty(g5, '__vexa_peer_connections', savedM);
  }
}

// 72. [M42] End-to-end for QA's case C: an UNSEALABLE track, re-enabled, is
//     recovered by the sweep — the cover the comment claims, now actually there.
{
  const u = makeUnsealableTrack();
  const g5 = globalThis as unknown as Record<string, unknown>;
  const savedZ = Object.getOwnPropertyDescriptor(g5, '__vexa_zoom_peer_connections');
  const savedLock = Object.getOwnPropertyDescriptor(g5, '__vexa_zoom_audio_lock');
  delete g5.__vexa_zoom_audio_lock;
  delete g5.__vexa_peer_connections;
  g5.__vexa_zoom_peer_connections = [peerWith([u.track])];
  try {
    const lock = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    assertEqual([lock.tracksLocked, lock.errors], [0, 1], '[M42] unsealable: locked=0, errors=1 (routes to the sweep)');
    u.track.enabled = true;
    assertEqual(u.real(), true, '[M42] CONTROL: an unsealable track really can be re-enabled');
    const sweep = silenceOutboundAudioTracks(false);
    assertEqual(sweep.tracksDisabled, 1, '[M42] the fallback sweep RECOVERS it (was 0 before the fix)');
    assertEqual(u.real(), false, '[M42] audio is silenced again');
  } finally {
    delete g5.__vexa_zoom_audio_lock;
    delete g5.__vexa_zoom_peer_connections;
    if (savedZ) Object.defineProperty(g5, '__vexa_zoom_peer_connections', savedZ);
    if (savedLock) Object.defineProperty(g5, '__vexa_zoom_audio_lock', savedLock);
  }
}

console.log('\n=== Item A: preview mute must not be skipped on a brittle label ===');

// 73. [M43] The preview step now reads state the same tolerant way the in-meeting
//     path does. Source guard, because it is Playwright code: the exact-match
//     `=== 'Mute'` must be gone, and a muted reading must NOT click.
{
  const joinSrc = readZoomWebSource('join.ts');
  assertEqual(joinSrc.includes("muteAriaLabel === 'Mute'"), false, "[M43] the brittle exact-match `=== 'Mute'` is gone");
  assertEqual(joinSrc.includes('readZoomMicState(probe)'), true, '[M43] the preview step uses the tolerant reader');
  const previewIdx = joinSrc.indexOf('readZoomMicState(probe)');
  const tail = joinSrc.slice(previewIdx);
  assertEqual(tail.includes("if (reading.kind === 'unmuted') {"), true, '[M43] it clicks ONLY on a confident unmuted reading');
  assertEqual(tail.includes('NOT clicking (a click here would unmute)'), true, '[M43] and explains why a muted reading is not clicked (toggle hazard)');
  assertEqual(tail.includes('unreadable'), true, '[M43] an unreadable state is logged, not silently skipped');
}

// 74. [M43] The same readings the preview step branches on, exercised directly —
//     so the branch conditions are covered by behaviour, not only by grep.
assertEqual(readZoomMicState(probe({ ariaLabel: 'Mute' })).kind, 'unmuted', '[M43] preview "Mute" => unmuted => WILL click');
assertEqual(readZoomMicState(probe({ ariaLabel: 'Unmute' })).kind, 'muted', '[M43] preview "Unmute" => muted => will NOT click');
assertEqual(readZoomMicState(probe({ ariaLabel: 'audio' })).kind, 'not-mute-toggle', '[M43] preview "audio" => unreadable => will NOT click');
assertEqual(readZoomMicState(probe({ ariaLabel: 'mute my microphone' })).kind, 'unmuted', '[M43] preview label variant still resolves');

console.log('\n=== N1: ZOOM_AUDIO_LOCK kill switch ===');

// 63. [M37] DEFAULT IS ON. This is the assertion that matters most: if the
//     default ever flips, an unprotected bot ships silently. Unset, empty and
//     unrecognised values must all mean ENABLED — a typo must never disable the
//     seal.
assertEqual(parseZoomAudioLockEnv(undefined), true, '[M37] DEFAULT: unset => seal ENABLED');
assertEqual(parseZoomAudioLockEnv(''), true, '[M37] DEFAULT: empty => seal ENABLED');
assertEqual(parseZoomAudioLockEnv('   '), true, '[M37] DEFAULT: whitespace => seal ENABLED');
for (const typo of ['offf', 'nope', 'disable', 'true', '1', 'on', 'yes', 'ON']) {
  assertEqual(parseZoomAudioLockEnv(typo), true, `[M37] unrecognised/affirmative "${typo}" => seal ENABLED (a typo must not disable it)`);
}

// 64. [M37] The documented opt-outs, case- and whitespace-insensitive.
for (const off of ['off', 'OFF', ' Off ', '0', 'false', 'FALSE', 'no', 'disabled']) {
  assertEqual(parseZoomAudioLockEnv(off), false, `[M37] "${off}" => seal DISABLED`);
}

// 65. [M38] SWITCH OFF: no seal, no at-birth patches — but the track is STILL
//     disabled, and an unmute is genuinely possible again. That is the pre-seal
//     behaviour, i.e. the configuration proven not to disturb audio-join.
{
  const mic = makeTrack('audio');
  const pcProto: FakePcProto & { addTransceiver?: unknown } = {
    addTrack: (t: unknown) => t,
    addTransceiver: (t: unknown) => t,
  };
  const originalAddTrack = pcProto.addTrack;
  withGlobals(
    { RTCPeerConnection: makeFakePcCtor(pcProto as Record<string, unknown>), __vexa_peer_connections: [peerWith([mic.track])] },
    () => {
      const r = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: false });
      assertEqual(r.sealEnabled, false, '[M38] the result reports the seal as disabled');
      assertEqual(mic.real(), false, '[M38] the track is STILL disabled (enabled=false is retained)');
      assertEqual(pcProto.addTrack === originalAddTrack, true, '[M38] addTrack is NOT patched');
      assertEqual(
        [r.patchedAddTrack, r.patchedAddTransceiver, r.patchedReplaceTrack, r.patchedGetUserMedia],
        [false, false, false, false],
        '[M38] NONE of the at-birth patches are installed',
      );
      // The honest consequence, asserted rather than glossed:
      mic.track.enabled = true;
      assertEqual(mic.real(), true, '[M38] HONEST LIMIT: with the switch off an unmute CAN succeed');
    },
  );
}

// 66. [M38] ...but the constructor registry patch IS still installed, because
//     without it `enabled = false` would have no senders to act on in the default
//     configuration and OFF would degrade to DOM-mute-only.
{
  const pcProto: FakePcProto = { addTrack: (t: unknown) => t };
  withGlobals({ RTCPeerConnection: makeFakePcCtor(pcProto as Record<string, unknown>) }, () => {
    const g4 = globalThis as unknown as Record<string, unknown>;
    delete g4.__vexa_zoom_peer_connections;
    const r = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: false });
    assertEqual(r.patchedConstructor, true, '[M38] the ctor registry patch survives the kill switch (it is observationally inert)');
    const Ctor = g4.RTCPeerConnection as new () => object;
    const pc = new Ctor();
    assertEqual((g4.__vexa_zoom_peer_connections as unknown[]).includes(pc), true, '[M38] so senders remain enumerable for the enabled=false sweep');
    delete g4.__vexa_zoom_peer_connections;
  });
}

// 67. [M39] voiceAgentEnabled bypasses everything INDEPENDENTLY of the switch —
//     both switch states must leave a voice agent able to transmit.
for (const sealEnabled of [true, false]) {
  const mic = makeTrack('audio');
  withGlobals({ __vexa_peer_connections: [peerWith([mic.track])] }, () => {
    const r = installOutboundAudioLockInPage({ voiceAgentEnabled: true, sealEnabled });
    assertEqual(r.skippedVoiceAgent, true, `[M39] voice agent bypasses the lock (sealEnabled=${sealEnabled})`);
    assertEqual(mic.real(), true, `[M39] voice agent audio stays ENABLED (sealEnabled=${sealEnabled})`);
  });
}

// 68. [M40] The mode must be visible in the log line, in BOTH states — a silent
//     kill switch is worse than none.
{
  const on = describeOutboundAudioLock({
    sealEnabled: true, skippedVoiceAgent: false, alreadyInstalled: false, registryPresent: true,
    tracksLocked: 1, blockedUnmutes: 0, tracksVerified: 0, tracksResealed: 0, patchedConstructor: true,
    patchedAddTrack: true, patchedAddTransceiver: true, patchedReplaceTrack: true, patchedGetUserMedia: true, errors: 0,
  });
  assertEqual(on.includes('tracksLocked=1') && !on.includes('SEAL DISABLED'), true, '[M40] seal ON: the description reports the locked track');
  const off = describeOutboundAudioLock({
    sealEnabled: false, skippedVoiceAgent: false, alreadyInstalled: false, registryPresent: true,
    tracksLocked: 1, blockedUnmutes: 0, tracksVerified: 0, tracksResealed: 0, patchedConstructor: true,
    patchedAddTrack: false, patchedAddTransceiver: false, patchedReplaceTrack: false, patchedGetUserMedia: false, errors: 0,
  });
  assertEqual(off.includes('SEAL DISABLED by ZOOM_AUDIO_LOCK'), true, '[M40] seal OFF: the description says so explicitly');
  assertEqual(off.includes('an unmute CAN succeed'), true, '[M40] seal OFF: and states the consequence plainly');
}

// 69. [M41] Source guard: the decision must be logged at arm time, and read via
//     the memoised accessor (so it is read once per process, not per call).
//     MOVED 2026-09-02: the memo + log now live in zoomAudioLockMode(), which
//     returns three states; isZoomAudioSealEnabled() is a thin `mode === 'on'`.
//     This guard follows the logic rather than staying pointed at a function
//     that no longer holds it — a source guard aimed at the wrong function is
//     the "assertion pointed at the wrong state" shape, and it would have gone
//     quietly green forever.
{
  const src = readPrepareSource();
  const start = src.indexOf('export function zoomAudioLockMode');
  assertEqual(start > -1, true, '[M41] zoomAudioLockMode is present in prepare.ts');
  const fn = src.slice(start, src.indexOf('\n}\n', start));
  assertEqual(fn.includes('process.env.ZOOM_AUDIO_LOCK'), true, '[M41] the env var is read');
  assertEqual(fn.includes('zoomAudioSealDecision !== null'), true, '[M41] the decision is memoised (read once per process)');
  // Statement position, not merely "the text log( appears": a dead-coded
  // `void 0 && log(...)` satisfies a bare includes() check, and did survive that
  // weaker assertion during mutation testing.
  assertEqual(/\n  log\(/.test(fn), true, '[M41] the decision is LOGGED at arm time (a real statement, not dead-coded)');
  assertEqual(
    fn.includes('SEAL: ENABLED') && fn.includes('SEAL: DISABLED') && fn.includes('SEAL: HANDS OFF'),
    true,
    '[M41] ALL THREE modes are logged plainly — an unlogged mode is an unanswerable "was it on?"',
  );
  // The seal accessor must derive from the mode, not read the env a second time:
  // two independent reads can disagree and would log twice.
  const sealStart = src.indexOf('export function isZoomAudioSealEnabled');
  const sealFn = src.slice(sealStart, src.indexOf('\n}\n', sealStart));
  assertEqual(sealFn.includes("zoomAudioLockMode() === 'on'"), true, '[M41] the seal flag derives from the single memoised mode');
  assertEqual(sealFn.includes('process.env'), false, '[M41] and does NOT read the env a second time');
  const joinSrc = readZoomWebSource('join.ts');
  assertEqual(joinSrc.includes('sealEnabled: isZoomAudioSealEnabled()'), true, '[M41] join.ts threads the switch into the page-load arm');
}

console.log('\n=== BLOCKER 1: a de-sealed track must be RE-SEALED ===');

// 56. [M32] THE BLOCKER. The seal is `configurable: true`, so it can be removed.
//     The old code short-circuited on WeakSet membership, so once removed the
//     track was never re-sealed: `enabled` went back to true and audio
//     transmitted indefinitely, while the guard reported errors:0 so even its
//     fallback sweep never fired. Reproduces the QA gate's exact sequence.
{
  const mic = makeTrack('audio');
  withGlobals({ __vexa_peer_connections: [peerWith([mic.track])] }, () => {
    // 1. after install
    const first = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    assertEqual([first.tracksLocked, mic.real()], [1, false], '[M32] step 1: sealed, real audio stopped');

    // 2. unmute attempt while sealed
    mic.track.enabled = true;
    assertEqual(mic.real(), false, '[M32] step 2: seal holds against an unmute attempt');

    // 3. seal REMOVED, then unmute -> audio live
    delete (mic.track as { enabled?: boolean }).enabled;
    mic.track.enabled = true;
    assertEqual(mic.real(), true, '[M32] step 3: CONTROL — with the seal removed the track really does go live');

    // 4. guard re-install MUST recover it
    const second = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    assertEqual(mic.real(), false, '[M32] step 4: the re-install RE-SEALS and stops the audio again');
    assertEqual(second.tracksResealed, 1, '[M32] step 4: the re-seal is reported (tracksResealed=1)');

    // 5. and the restored seal holds
    mic.track.enabled = true;
    assertEqual(mic.real(), false, '[M32] step 5: the restored seal holds against a further unmute attempt');
  });
}

// 57. [M32] A seal that is STILL intact is verified, not re-applied — so the
//     counters stay meaningful across a long meeting's guard ticks.
{
  const mic = makeTrack('audio');
  withGlobals({ __vexa_peer_connections: [peerWith([mic.track])] }, () => {
    installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    const again = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    assertEqual([again.tracksVerified, again.tracksResealed], [1, 0], '[M32] an intact seal is VERIFIED, not re-sealed');
  });
}

// 58. [M32] A foreign accessor is not mistaken for our seal — the marker on the
//     getter is what identifies it, not merely "there is a getter here".
{
  const mic = makeTrack('audio');
  withGlobals({ __vexa_peer_connections: [peerWith([mic.track])] }, () => {
    let hidden = true;
    Object.defineProperty(mic.track, 'enabled', {
      configurable: true,
      get: () => hidden,
      set: (v: boolean) => { hidden = v; },
    });
    const r = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    assertEqual(r.tracksLocked, 1, '[M32] a non-lock accessor is replaced by a real seal');
    mic.track.enabled = true;
    assertEqual(mic.track.enabled, false, '[M32] and that seal refuses an unmute');
  });
}

console.log('\n=== BLOCKER 2: the DEFAULT bot configuration (registry ABSENT) ===');

// 59. [M33] THE OTHER BLOCKER. `__vexa_peer_connections` is written only by
//     services/screen-content.ts's getVirtualCameraInitScript, installed solely
//     when cameraEnabled is true — default FALSE. So in the standard recorder
//     configuration the registry does NOT exist, and the existing-track step
//     sealed nothing. The mic track must therefore be caught AT BIRTH by the
//     getUserMedia patch, with NO registry present at all.
{
  const mic = makeTrack('audio');
  const stream = { getAudioTracks: () => [mic.track] };
  const md = { getUserMedia: (_c: unknown) => Promise.resolve(stream) };
  const g2 = globalThis as unknown as Record<string, unknown>;
  const savedNav = Object.getOwnPropertyDescriptor(g2, 'navigator');
  const savedLock = Object.getOwnPropertyDescriptor(g2, '__vexa_zoom_audio_lock');
  const savedZoomReg = Object.getOwnPropertyDescriptor(g2, '__vexa_zoom_peer_connections');
  delete g2.__vexa_zoom_audio_lock;
  delete g2.__vexa_zoom_peer_connections;
  delete g2.__vexa_peer_connections; // DEFAULT CONFIG: no shared registry
  Object.defineProperty(g2, 'navigator', { value: { mediaDevices: md }, writable: true, configurable: true });
  try {
    const r = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    assertEqual(r.registryPresent, false, '[M33] DEFAULT CONFIG: no peer-connection registry exists');
    assertEqual(r.patchedGetUserMedia, true, '[M33] the getUserMedia patch is installed anyway');
    // The mic track is acquired AFTER the patch — exactly what arming at page
    // load guarantees, since the patch precedes every Zoom script.
    void (md.getUserMedia as (c: unknown) => Promise<unknown>)({ audio: true }).then(() => {
      assertEqual(mic.real(), false, '[M33] GUARANTEE WITHOUT A REGISTRY: the mic track is sealed at birth');
      mic.track.enabled = true;
      assertEqual(mic.real(), false, '[M33] and it cannot be unmuted');
    });
  } finally {
    delete g2.__vexa_zoom_audio_lock;
    delete g2.__vexa_zoom_peer_connections;
    if (savedZoomReg) Object.defineProperty(g2, '__vexa_zoom_peer_connections', savedZoomReg);
    if (savedLock) Object.defineProperty(g2, '__vexa_zoom_audio_lock', savedLock);
    if (savedNav) Object.defineProperty(g2, 'navigator', savedNav);
    else delete g2.navigator;
  }
}

// 60. [M34] The constructor patch builds a ZOOM-LOCAL registry so senders can be
//     enumerated without Meet's. It must NOT write Meet's global name.
{
  const pcProto: FakePcProto = { addTrack: (t: unknown) => t };
  withGlobals({ RTCPeerConnection: makeFakePcCtor(pcProto as Record<string, unknown>) }, () => {
    const g3 = globalThis as unknown as Record<string, unknown>;
    delete g3.__vexa_zoom_peer_connections;
    delete g3.__vexa_peer_connections;
    const r = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    assertEqual(r.patchedConstructor, true, '[M34] the RTCPeerConnection constructor is patched');
    const Ctor = g3.RTCPeerConnection as new () => object;
    const pc = new Ctor();
    assertEqual(Array.isArray(g3.__vexa_zoom_peer_connections), true, '[M34] a ZOOM-LOCAL registry is created');
    assertEqual((g3.__vexa_zoom_peer_connections as unknown[]).includes(pc), true, '[M34] new peers are recorded in it');
    assertEqual('__vexa_peer_connections' in g3, false, "[M34] Meet's shared registry name is NEVER written");
    delete g3.__vexa_zoom_peer_connections;
  });
}

// 61. [M35] addTransceiver is a fourth attach path and must also seal at birth.
{
  const fresh = makeTrack('audio');
  let seenEnabled: boolean | null = null;
  const pcProto: FakePcProto & { addTransceiver?: unknown } = {
    addTrack: (t: unknown) => t,
    addTransceiver(track: { enabled: boolean }) { seenEnabled = track.enabled; return {}; },
  };
  withGlobals({ RTCPeerConnection: makeFakePcCtor(pcProto as Record<string, unknown>) }, () => {
    const r = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    assertEqual(r.patchedAddTransceiver, true, '[M35] addTransceiver is patched');
    (pcProto.addTransceiver as (t: unknown) => unknown)(fresh.track);
    assertEqual(fresh.real(), false, '[M35] a track attached via addTransceiver is sealed at birth');
    assertEqual(seenEnabled, false, '[M35] it was disabled BEFORE the original addTransceiver ran');
  });
}

console.log('\n=== W8: sealing-failure fallback (defineProperty throws) ===');

/**
 * A track whose `enabled` is WRITABLE but NON-CONFIGURABLE: assignment works, so
 * the audio can still be stopped, but Object.defineProperty throws. makeTrack()
 * always yields a configurable accessor, so none of the other cases ever reach
 * the catch branch — this fixture is the only one that executes it.
 */
function makeUnsealableTrack(): { track: { kind: string; enabled: boolean }; real: () => boolean } {
  const track = { kind: 'audio' } as { kind: string; enabled: boolean };
  Object.defineProperty(track, 'enabled', { value: true, writable: true, configurable: false, enumerable: true });
  return { track, real: () => track.enabled };
}

// 51. [M29] The fixture must genuinely throw, or the test below proves nothing.
{
  const u = makeUnsealableTrack();
  let threw = false;
  try {
    Object.defineProperty(u.track, 'enabled', { get: () => false, configurable: true });
  } catch {
    threw = true;
  }
  assertEqual(threw, true, '[M29] CONTROL: defineProperty really does throw on this fixture');
}

// 52. [M29] When sealing fails, silence is still applied — because `enabled =
//     false` runs BEFORE the seal — and the failure is REPORTED (errors > 0),
//     which is what routes the track to the guard's periodic re-sweep.
{
  const u = makeUnsealableTrack();
  withGlobals({ __vexa_peer_connections: [peerWith([u.track])] }, () => {
    const r = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    assertEqual(u.real(), false, '[M29] unsealable track is still DISABLED (step 1 ran before the seal threw)');
    assertEqual(r.errors, 1, '[M29] the sealing failure is counted (drives the guard re-sweep)');
    assertEqual(r.tracksLocked, 0, '[M29] it is NOT counted as locked — the lock genuinely did not take');
  });
}

// 53. An unsealable track really is revocable — this is the honest limit of the
//     fallback, asserted so nobody mistakes it for a guarantee.
{
  const u = makeUnsealableTrack();
  withGlobals({ __vexa_peer_connections: [peerWith([u.track])] }, () => {
    installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    u.track.enabled = true;
    assertEqual(u.real(), true, 'an UNSEALABLE track can be re-enabled — only the periodic sweep covers it');
  });
}

console.log('\n=== LAYER 3: stepZoomMuteWatcher (persistent visual mute) ===');

const WCFG = { confirmations: 2, cooldownMs: 30_000 };
const initial = zoomMuteWatcherInitialState;

// 35. A confident 'muted' reading does nothing and resets any streak.
{
  const r = stepZoomMuteWatcher({ consecutiveUnmuted: 1, lastClickAtMs: null, clicks: 0 }, 'muted', 1000, WCFG);
  assertEqual([r.action, r.state.consecutiveUnmuted], ['none', 0], "'muted' => no action, streak reset");
}

// 36. [M22] Non-confident readings NEVER click and never accumulate — the same
//     asymmetry readZoomMicState uses. Clicking an unidentified control could
//     UNMUTE a muted bot.
for (const kind of ['unknown', 'not-mute-toggle', 'not-mic-control', 'none'] as const) {
  const r = stepZoomMuteWatcher({ consecutiveUnmuted: 1, lastClickAtMs: null, clicks: 0 }, kind, 1000, WCFG);
  assertEqual([r.action, r.state.consecutiveUnmuted], ['none', 0], `[M22] '${kind}' => never clicks, streak reset`);
}

// 37. [M23] Confirmations are required: one 'unmuted' reading is not enough.
//     This is what absorbs a lagging aria-label instead of oscillating on it.
{
  const first = stepZoomMuteWatcher(initial, 'unmuted', 1000, WCFG);
  assertEqual([first.action, first.state.consecutiveUnmuted], ['none', 1], '[M23] 1st unmuted reading => no click yet');
  const second = stepZoomMuteWatcher(first.state, 'unmuted', 2000, WCFG);
  assertEqual(second.action, 'click', '[M23] 2nd consecutive unmuted reading => click');
  assertEqual([second.state.clicks, second.state.lastClickAtMs, second.state.consecutiveUnmuted], [1, 2000, 0], 'click recorded, streak reset');
}

// 38. An interrupted streak does not click — this is the anti-oscillation guard.
{
  const a = stepZoomMuteWatcher(initial, 'unmuted', 1000, WCFG);
  const b = stepZoomMuteWatcher(a.state, 'muted', 2000, WCFG);
  const c = stepZoomMuteWatcher(b.state, 'unmuted', 3000, WCFG);
  assertEqual(c.action, 'none', 'unmuted -> muted -> unmuted never reaches 2 CONSECUTIVE readings');
}

// 39. [M24] Cooldown: even sustained 'unmuted' cannot produce a click train.
{
  let st = stepZoomMuteWatcher(initial, 'unmuted', 1000, WCFG).state;
  st = stepZoomMuteWatcher(st, 'unmuted', 2000, WCFG).state; // clicked at 2000
  const soonA = stepZoomMuteWatcher(st, 'unmuted', 3000, WCFG);
  const soonB = stepZoomMuteWatcher(soonA.state, 'unmuted', 4000, WCFG);
  assertEqual(soonB.action, 'none', '[M24] second click refused inside the cooldown window');
  assertEqual(soonB.reason.includes('cooldown'), true, '[M24] the refusal names the cooldown');
  const laterA = stepZoomMuteWatcher(soonB.state, 'unmuted', 40_000, WCFG);
  assertEqual(laterA.action, 'click', '[M24] a click is allowed once the cooldown has expired');
  assertEqual(laterA.state.clicks, 2, 'click count accumulates');
}

// 40. Ten consecutive unmuted readings inside one cooldown produce exactly ONE
//     click. Stated as a bound because an even number of clicks would leave the
//     bot unmuted — the precise failure the one-shot latch exists to prevent.
{
  let st = initial;
  let clicks = 0;
  for (let i = 1; i <= 10; i++) {
    const r = stepZoomMuteWatcher(st, 'unmuted', i * 1000, WCFG);
    st = r.state;
    if (r.action === 'click') clicks++;
  }
  assertEqual(clicks, 1, 'ten unmuted readings within one cooldown => exactly one click (never an even number)');
}

/**
 * Locate prepare.ts for the source guards below. Both documented run locations
 * are tried (repo root and the core dir). If neither resolves this FAILS loudly
 * rather than skipping — a source guard that quietly stops running is worse than
 * no guard at all.
 */
function readZoomWebSource(file: string): string {
  const candidates = [
    `src/platforms/zoom/web/${file}`,
    `services/vexa-bot/core/src/platforms/zoom/web/${file}`,
    `vexa-fork/services/vexa-bot/core/src/platforms/zoom/web/${file}`,
  ];
  for (const rel of candidates) {
    try {
      return readFileSync(rel, 'utf8');
    } catch {
      /* try the next candidate */
    }
  }
  throw new Error(`could not locate ${file} from cwd=${process.cwd()} (tried: ${candidates.join(', ')})`);
}

function readPrepareSource(): string {
  return readZoomWebSource('prepare.ts');
}

/**
 * Strip comments from a source slice, for NEGATIVE assertions only.
 *
 * WHY: a guard of the form "this literal must NOT appear" is satisfied — or
 * broken — by explanatory prose. That has now bitten three separate assertions
 * in this file (`virtual_mic` in start.sh, `tts_sink`, and `el.textContent`),
 * every time because the comment ABOVE the code names the thing the code must
 * not do, which is exactly what a good comment does. So negative guards read
 * code only. Positive guards deliberately keep the comments, since some of them
 * pin that a REASON is recorded next to the code.
 *
 * Line comments and whole-line block comments only — enough for this file's
 * style, and it does not pretend to be a parser.
 */
/**
 * Extract the balanced `{ ... }` block that starts at `openIdx`.
 *
 * WHY: M82 proved "the sweep/lock/guard are inside the gate" with INDEX
 * ORDERING alone (`gateIdx < sweepIdx`), which stays green if the three calls
 * are moved BELOW the gate's closing brace — i.e. it did not prove containment
 * at all. Brace matching does. String and comment contents are not parsed; this
 * file's sources contain no unbalanced braces inside either, and a positive
 * control below proves the matcher can fail.
 */
function extractBraceBlock(src: string, openIdx: number): string | null {
  const first = src.indexOf('{', openIdx);
  if (first === -1) return null;
  let depth = 0;
  for (let i = first; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') {
      depth--;
      if (depth === 0) return src.slice(first, i + 1);
    }
  }
  return null; // unbalanced
}

function codeOnly(src: string): string {
  return src
    .split('\n')
    .filter((l) => {
      const t = l.trimStart();
      return !(t.startsWith('//') || t.startsWith('*') || t.startsWith('/*'));
    })
    .join('\n');
}

console.log('\n=== W4: source guards for the watcher driver (not unit-testable) ===');

// 54. [M30] startZoomMuteWatcher's body is Playwright + setInterval, so no unit
//     test can execute it. It had a real defect: it probed WITHOUT revealing the
//     footer, and probeZoomMicCandidates skips zero-area elements — so with the
//     toolbar auto-hidden every pass found nothing and the watcher was inert,
//     silently, forever. A source guard is the only check available here; it is
//     weaker than a behavioural test and is not presented as equivalent.
{
  const src = readPrepareSource();
  const start = src.indexOf('export function startZoomMuteWatcher');
  assertEqual(start > -1, true, 'startZoomMuteWatcher is present in prepare.ts');
  const body = src.slice(start);
  const end = body.indexOf('\n}\n');
  const fn = body.slice(0, end);

  assertEqual(fn.includes('await revealZoomFooter(page)'), true, '[M30] the watcher reveals the footer before probing (else every pass is blind)');
  // Match the CALL, not the bare identifier: the explanatory comment above the
  // reveal also names probeZoomMicCandidates, and matching that made this
  // assertion fail against correct code.
  assertEqual(
    fn.indexOf('await revealZoomFooter(page)') < fn.indexOf('await probeZoomMicCandidates(page'),
    true,
    '[M30] the reveal happens BEFORE the probe, not after',
  );
  assertEqual(fn.includes('noCandidatePasses'), true, '[M30] a no-candidate pass is tracked, not swallowed');
  assertEqual(
    fn.includes('Visual re-mute is INACTIVE'),
    true,
    '[M30] and it is LOGGED — an invisible no-op is the worst outcome for a watcher',
  );
  // W11: the same line must NOT overstate. "No actionable control" is a fact
  // about the DOM; the bot's appearance is the participant list's business, and
  // on 2026-09-02 the two disagreed for a whole meeting.
  assertEqual(
    fn.includes('NOT evidence the bot appears unmuted'),
    true,
    '[W11] and it explicitly declines to claim the bot appears unmuted',
  );
  assertEqual(
    fn.includes('no ACTIONABLE mute control'),
    true,
    '[W11] and says "no ACTIONABLE control", not "no control" — the live DOM had four',
  );
}

// 55. The guard/watcher must forward the closure flag, never a hardcoded false:
//     if the voice-agent early return were ever removed, a hardcoded false would
//     silently force-mute an agent that legitimately transmits.
{
  const src = readPrepareSource();
  const start = src.indexOf('export function startZoomOutboundAudioGuard');
  const fn = src.slice(start, src.indexOf('\n}\n', start));
  assertEqual(fn.includes('installZoomOutboundAudioLock(page, voiceAgentEnabled)'), true, 'the guard forwards voiceAgentEnabled to the lock');
  assertEqual(fn.includes('sweepZoomOutboundAudio(page, voiceAgentEnabled)'), true, 'the guard forwards voiceAgentEnabled to the sweep');
  assertEqual(fn.includes('(page, false)'), false, 'no hardcoded false remains in the guard');
}

console.log('\n=== BLOCKER 2 (timing): the lock must be armed BEFORE navigation ===');

// 62. [M36] The part of BLOCKER 2 that no unit test can execute: WHERE the lock
//     is installed. Arming it at afterAdmission was the defect — the bot joins
//     audio during preview/join, so the real mic track already exists by then,
//     and finding an existing track needs a peer registry that does not exist in
//     the default configuration. Arming at page load means no outbound audio
//     track can pre-exist the patches. This is an ordering property of join.ts,
//     so a source guard is the only available check; it is weaker than a
//     behavioural test and is not offered as equivalent.
{
  const src = readZoomWebSource('join.ts');
  assertEqual(src.includes('addInitScript(installOutboundAudioLockInPage'), true, '[M36] join.ts arms the lock via addInitScript');
  const armIdx = src.indexOf('addInitScript(installOutboundAudioLockInPage');
  const gotoIdx = src.indexOf('page.goto(');
  assertEqual(armIdx > -1 && gotoIdx > -1, true, '[M36] both the arm and the navigation are present');
  assertEqual(armIdx < gotoIdx, true, '[M36] the lock is armed BEFORE the first page.goto — otherwise the mic track predates the patches');
  // The voice agent must not be locked silent by the page-load arm either.
  assertEqual(src.includes('botConfig.voiceAgentEnabled'), true, '[M36] the page-load arm is gated on voiceAgentEnabled');
}

console.log('\n=== W5: guard observability — reportZoomAudioGuardTick (Defect A) ===');

// Fixture numbers are the REAL ones from the live 5-minute meeting of
// 2026-09-02 06:45:19: pcs=3, audioSenders=0, everything patched, 0 errors.
// A healthy tick therefore looks exactly like the run that produced only ONE
// guard line in five minutes — which is the situation these tests describe.
const healthyLock = (over: Partial<OutboundAudioLockResult> = {}): OutboundAudioLockResult => ({
  sealEnabled: true,
  skippedVoiceAgent: false,
  alreadyInstalled: true,
  registryPresent: true,
  tracksLocked: 0,
  blockedUnmutes: 0,
  tracksVerified: 0,
  tracksResealed: 0,
  patchedConstructor: true,
  patchedAddTrack: true,
  patchedAddTransceiver: true,
  patchedReplaceTrack: true,
  patchedGetUserMedia: true,
  errors: 0,
  ...over,
});

const healthySweep = (over: Partial<OutboundAudioSweepResult> = {}): OutboundAudioSweepResult => ({
  skippedVoiceAgent: false,
  registryPresent: true,
  peerConnections: 3,
  audioSendersFound: 0,
  tracksDisabled: 0,
  alreadyDisabled: 0,
  errors: 0,
  ...over,
});

// 56. [M44] THE DEFECT. A no-change tick used to log NOTHING, so 30 of the 31
//     ticks in the live meeting were silent and nothing could be said about
//     them. Every tick must now produce a line, and that line must carry the
//     counters — above all audioSenders, which the log never contained.
{
  const r = reportZoomAudioGuardTick(healthyLock(), healthySweep());
  assertEqual(r.message.includes('audioSenders=0'), true, '[M44] the heartbeat carries audioSenders (the number the live log never had)');
  assertEqual(r.message.includes('tracksLocked=0'), true, '[M44] the heartbeat carries tracksLocked');
  assertEqual(r.message.includes('verified=0'), true, '[M44] the heartbeat carries verified');
  assertEqual(r.message.includes('resealed=0'), true, '[M44] the heartbeat carries resealed');
  assertEqual(r.message.includes('blockedUnmutes=0'), true, '[M44] the heartbeat carries blockedUnmutes');
  assertEqual(r.message.includes('registry=present'), true, '[M44] the heartbeat carries the registry state');
  assertEqual(r.message.includes('patched=[ctor,addTrack,addTransceiver,replaceTrack,getUserMedia]'), true, '[M44] the heartbeat carries which patches are installed');
  assertEqual(r.message.includes('errors=0'), true, '[M44] the heartbeat carries the error count');
  // The control: a fully healthy tick is NOT a warning. If `warn` were hardwired
  // true this goes red, so the warn assertions below cannot pass vacuously.
  assertEqual(r.warn, false, '[M44] a healthy tick is informational, not a warning');
}

// 57. [M45] The signature is what buys an IMMEDIATE emit on a real change
//     instead of waiting up to a heartbeat. Table-driven: change exactly one
//     field and the signature must move. Each row fails if that field is
//     dropped from the signature — nothing else differs between the two inputs.
{
  const rows: Array<[string, Partial<OutboundAudioLockResult>]> = [
    ['sealEnabled', { sealEnabled: false }],
    ['registryPresent', { registryPresent: false }],
    ['tracksLocked', { tracksLocked: 1 }],
    ['tracksVerified', { tracksVerified: 1 }],
    ['tracksResealed', { tracksResealed: 1 }],
    ['blockedUnmutes', { blockedUnmutes: 1 }],
    ['errors', { errors: 1 }],
    ['patchedConstructor', { patchedConstructor: false }],
    ['patchedAddTrack', { patchedAddTrack: false }],
    ['patchedAddTransceiver', { patchedAddTransceiver: false }],
    ['patchedReplaceTrack', { patchedReplaceTrack: false }],
    ['patchedGetUserMedia', { patchedGetUserMedia: false }],
  ];
  const base = reportZoomAudioGuardTick(healthyLock(), healthySweep()).signature;
  for (const [field, over] of rows) {
    const moved = reportZoomAudioGuardTick(healthyLock(over), healthySweep()).signature;
    assertEqual(moved !== base, true, `[M45] lock.${field} is in the signature (a change emits at once)`);
  }
  const sweepRows: Array<[string, Partial<OutboundAudioSweepResult>]> = [
    ['registryPresent', { registryPresent: false }],
    ['audioSendersFound', { audioSendersFound: 1 }],
    ['tracksDisabled', { tracksDisabled: 1 }],
    ['errors', { errors: 1 }],
  ];
  for (const [field, over] of sweepRows) {
    const moved = reportZoomAudioGuardTick(healthyLock(), healthySweep(over)).signature;
    assertEqual(moved !== base, true, `[M45] sweep.${field} is in the signature (a change emits at once)`);
  }
  // Identical inputs must produce an identical signature, or every tick emits
  // and the heartbeat degenerates into the 10s flood the collapser exists to
  // prevent. This is the other half of M45.
  assertEqual(
    reportZoomAudioGuardTick(healthyLock(), healthySweep()).signature === base,
    true,
    '[M45] an unchanged situation keeps the SAME signature (so it heartbeats, not floods)',
  );
}

// 58. [M46] A tick that could not read the page must SAY so. The old code did
//     `if (!lock) return;` — a silent return, i.e. the same invisibility as the
//     no-change tick but for a genuinely broken pass.
{
  const r = reportZoomAudioGuardTick(null, healthySweep());
  assertEqual(r.signature, 'lock-unavailable', '[M46] an unreadable lock has its own signature');
  assertEqual(r.warn, true, '[M46] an unreadable lock is a WARNING, not a silent return');
  assertEqual(r.message.includes('could not read the lock'), true, '[M46] and the line says what failed');
}

// 59. [M47] audioSenders comes from the sweep, so a sweep that could not run
//     must degrade to "unreadable" — never to a fabricated 0. Reporting 0 here
//     would be indistinguishable from the live "no outbound sender" reading and
//     would corrupt the one number this change exists to surface.
{
  const r = reportZoomAudioGuardTick(healthyLock(), null);
  assertEqual(r.message.includes('audioSenders=unreadable'), true, '[M47] a missing sweep reads "unreadable", never 0');
  assertEqual(r.message.includes('audioSenders=0'), false, '[M47] and never fabricates a zero');
  assertEqual(r.warn, true, '[M47] an unreadable sweep is a warning');
}

// 60. [M48] Registry absent = the sweep saw no peer connections at all, so its
//     audioSendersFound of 0 means "did not look", not "none exist". Those two
//     must never print the same.
{
  const r = reportZoomAudioGuardTick(healthyLock(), healthySweep({ registryPresent: false }));
  assertEqual(r.message.includes('registry ABSENT'), true, '[M48] an absent registry is named explicitly');
  assertEqual(r.message.includes('audioSenders=0'), false, '[M48] "did not look" never prints as audioSenders=0');
  assertEqual(r.warn, true, '[M48] an absent registry is a warning');
}

// 61. [M49] The case the backstop exists for: the sweep DISABLED a live
//     outbound audio track, meaning the seal had missed it. Previously this was
//     logged only when lock.errors > 0, so a miss with errors=0 was invisible.
{
  const r = reportZoomAudioGuardTick(healthyLock(), healthySweep({ audioSendersFound: 1, tracksDisabled: 1 }));
  assertEqual(r.warn, true, '[M49] a sweep that had to disable a track is a WARNING (the seal missed it)');
  assertEqual(r.message.includes('audioSenders=1'), true, '[M49] and the sender count is reported');
  assertEqual(r.message.includes('sweepDisabled=1'), true, '[M49] and how many it had to disable');
}

// 62. [M50] Remaining warning triggers, each with everything else healthy so
//     the trigger under test is the only difference.
{
  assertEqual(reportZoomAudioGuardTick(healthyLock({ errors: 1 }), healthySweep()).warn, true, '[M50] lock errors => warning');
  assertEqual(reportZoomAudioGuardTick(healthyLock({ registryPresent: false }), healthySweep()).warn, true, '[M50] absent lock registry => warning');
  assertEqual(reportZoomAudioGuardTick(healthyLock({ sealEnabled: false }), healthySweep()).warn, true, '[M50] seal disabled by the kill switch => warning on every heartbeat');
  assertEqual(reportZoomAudioGuardTick(healthyLock(), healthySweep({ errors: 1 })).warn, true, '[M50] sweep errors => warning');
  assertEqual(
    reportZoomAudioGuardTick(healthyLock({ sealEnabled: false }), healthySweep()).message.includes('SEAL DISABLED'),
    true,
    '[M50] and the seal-off heartbeat says so in full',
  );
}

// 63. [M51] audioSenders=0 is reported as a FACT and deliberately not
//     classified. Whether a bot with no outbound audio sender can be muted from
//     Zoom's toolbar is under separate investigation; asserting a verdict here
//     would be the same overstatement this change removes.
{
  const r = reportZoomAudioGuardTick(healthyLock(), healthySweep());
  assertEqual(r.warn, false, '[M51] audioSenders=0 is not itself treated as a fault (that question is open)');
  assertEqual(r.message.includes('audioSenders=0'), true, '[M51] but it is always visible in the heartbeat');
}

console.log('\n=== W5: source guards for the guard driver (setInterval + Playwright) ===');

// 64. startZoomOutboundAudioGuard's body is setInterval + Playwright and cannot
//     be executed by a unit test. These are SOURCE guards: weaker than a
//     behavioural test and NOT offered as equivalent. Each one names the exact
//     edit it would catch.
{
  const src = readPrepareSource();
  const start = src.indexOf('export function startZoomOutboundAudioGuard');
  assertEqual(start > -1, true, 'startZoomOutboundAudioGuard is present in prepare.ts');
  const fn = src.slice(start, src.indexOf('\n}\n', start));

  // The two edits that would restore Defect A exactly.
  assertEqual(fn.includes('if (lock.errors > 0)'), false, '[M52] the sweep is NO LONGER gated on lock.errors (it is the only source of audioSenders)');
  assertEqual(fn.includes('if (!lock) return;'), false, '[M52] a failed lock read is reported, not silently returned');
  assertEqual(fn.includes('newlyLocked'), false, '[M52] the "only log a change" rule is gone (it made 30 of 31 ticks silent)');

  // Statement-position, exactly as written — the M41 lesson: a guard matching a
  // bare identifier survives `void 0 && log(...)`.
  assertEqual(fn.includes('const sweep = await sweepZoomOutboundAudio(page, voiceAgentEnabled);'), true, '[M52] every tick sweeps');
  assertEqual(fn.includes('const report = reportZoomAudioGuardTick(lock, sweep);'), true, '[M52] every tick builds a report from BOTH the lock and the sweep');
  assertEqual(
    fn.includes("const line = heartbeat.consider('zoom-audio-guard', report.signature, report.message, Date.now());"),
    true,
    '[M52] the report goes through the repeat collapser (heartbeat, not flood)',
  );
  assertEqual(fn.includes('if (line) emit(line, report.warn);'), true, '[M52] and the collapsed line is actually LOGGED, at the reported level');
  assertEqual(fn.includes('const heartbeat = createRepeatCollapser(heartbeatMs);'), true, '[M52] the collapser interval is the injected heartbeatMs, not hardcoded');
  assertEqual(fn.includes("heartbeat.flush('zoom-audio-guard', Date.now());"), true, '[M52] stop() flushes, so the final suppressed run is not lost');
  // The emit helper must be a real statement that reaches log(), and must carry
  // the WARNING prefix — otherwise a warning heartbeat is indistinguishable from
  // a healthy one in the log.
  assertEqual(fn.includes('log(warn ? `[Zoom Web] WARNING: ${line}` : `[Zoom Web] ${line}`)'), true, '[M52] a warning tick is prefixed WARNING in the log');
  // Ordering: the lock re-assert must still precede the sweep, or the sweep
  // reports senders the lock has not yet had a chance to seal.
  assertEqual(
    fn.indexOf('await installZoomOutboundAudioLock(page, voiceAgentEnabled)') < fn.indexOf('await sweepZoomOutboundAudio(page, voiceAgentEnabled)'),
    true,
    '[M52] the lock re-assert runs BEFORE the sweep',
  );
}

console.log('\n=== W6: the watcher click is VERIFIED — reportZoomMuteClick (Defect B) ===');

// The evidence strings are the real ones from 2026-09-02, and they are
// DELIBERATELY DIFFERENT before vs after: with one shared string an assertion
// that "the message mentions svgaudiounmuted" would pass no matter which
// reading it came from — a control accidentally equal to the ambient value.
const clickedOn: ZoomMicSelection = {
  candidate: candidate('button.join-audio-container__btn', 0, {
    ariaLabel: 'audio',
    className: 'footer-button-base__button ax-outline join-audio-container__btn',
    descendantClassNames: ['SvgAudioUnmuted'],
  }),
  reading: { kind: 'unmuted', evidence: 'class hint "svgaudiounmuted"' },
};
const readBackMuted: ZoomMicSelection = {
  candidate: clickedOn.candidate,
  reading: { kind: 'muted', evidence: 'class hint "svgaudiomuted"' },
};
const readBackUnmuted: ZoomMicSelection = {
  candidate: clickedOn.candidate,
  reading: { kind: 'unmuted', evidence: 'aria-label "mute" offers mute' },
};

// 65. [M53] A click followed by a MUTED re-read is the only case that may be
//     reported as a mute.
{
  const r = reportZoomMuteClick({
    reason: 'confirmed unmuted 2x — re-muting (click #1)',
    before: clickedOn,
    after: readBackMuted,
    afterDetail: 'digest-not-used-on-success',
    priorIneffectiveClicks: 0,
  });
  assertEqual(r.verdict, 'muted', '[M53] muted re-read => verdict muted');
  assertEqual(r.warn, false, '[M53] a confirmed mute is not a warning');
  assertEqual(r.message.includes('CONFIRMED the bot muted'), true, '[M53] and the line reports an OBSERVATION');
  assertEqual(r.message.includes('NO EFFECT'), false, '[M53] and does not also claim failure');
  // Both readings must be present and distinguishable: the trigger and the
  // re-read are different facts and the log has to separate them.
  assertEqual(r.message.includes('class hint "svgaudiounmuted"'), true, '[M53] the line carries the reading that TRIGGERED the click');
  assertEqual(r.message.includes('class hint "svgaudiomuted"'), true, '[M53] and the reading taken AFTER it');
}

// 66. [M54] THE DEFECT. The old code logged `Mute watcher re-muted the bot`
//     unconditionally on the line after .click(), with no re-read. On
//     2026-09-02 it printed that twice while the control stayed
//     svgaudiounmuted, and the user muted the bot by hand. A click that did not
//     mute must now read as a failure, at warning level.
{
  const r = reportZoomMuteClick({
    reason: 'confirmed unmuted 2x — re-muting (click #1)',
    before: clickedOn,
    after: readBackUnmuted,
    afterDetail: 'button.join-audio-container__btn[0] aria-label="audio" aria-pressed=absent -> unmuted (class hint "svgaudiounmuted")',
    priorIneffectiveClicks: 0,
  });
  assertEqual(r.verdict, 'still-unmuted', '[M54] an unmuted re-read => verdict still-unmuted');
  assertEqual(r.warn, true, '[M54] an ineffective click is a WARNING');
  assertEqual(r.message.includes('STILL reads unmuted'), true, '[M54] and the line says the click had no effect');
  assertEqual(r.message.includes('NO EFFECT'), true, '[M54] in words that cannot be read as success');
  assertEqual(r.message.includes('re-muted the bot'), false, '[M54] the false-success wording is gone');
  assertEqual(r.message.includes('aria-label "mute" offers mute'), true, '[M54] the post-click reading is quoted');
  assertEqual(
    r.message.includes('button.join-audio-container__btn[0] aria-label="audio"'),
    true,
    '[M54] and the FULL post-click probe digest is attached, so the next run can show WHY',
  );
}

// 67. [M55] No confident reading after the click => UNKNOWN. Not a success, and
//     not counted as a proven failure either. `after: null` is exactly what
//     selectZoomMicToggle returns when nothing reads confidently, so this is a
//     value the real producer emits. (The same branch also covers a
//     non-confident reading kind, which selectZoomMicToggle cannot produce and
//     is therefore not asserted here.)
{
  const r = reportZoomMuteClick({
    reason: 'confirmed unmuted 2x — re-muting (click #2)',
    before: clickedOn,
    after: null,
    afterDetail: 'no mic-control candidates matched any selector',
    priorIneffectiveClicks: 1,
  });
  assertEqual(r.verdict, 'unreadable', '[M55] no confident re-read => verdict unreadable');
  assertEqual(r.warn, true, '[M55] an unverifiable click is a warning');
  assertEqual(r.message.includes('could NOT verify'), true, '[M55] and says the outcome is unverified');
  assertEqual(r.message.includes('UNKNOWN'), true, '[M55] naming the state as unknown rather than muted');
  assertEqual(r.message.includes('ineffective click'), false, '[M55] and does NOT tally it as a proven failure');
  assertEqual(r.message.includes('no mic-control candidates matched any selector'), true, '[M55] the probe digest is attached');
}

// 68. [M56] WHICH element was clicked. The old line carried only
//     reading.evidence, so with seven candidate selectors in play the clicked
//     element could not be recovered from the log at all.
{
  for (const after of [readBackMuted, readBackUnmuted, null]) {
    const r = reportZoomMuteClick({
      reason: 'confirmed unmuted 2x — re-muting (click #1)',
      before: clickedOn,
      after,
      afterDetail: 'd',
      priorIneffectiveClicks: 0,
    });
    assertEqual(
      r.message.includes('clicked button.join-audio-container__btn[0]'),
      true,
      `[M56] the clicked selector[index] is named (verdict ${r.verdict})`,
    );
    assertEqual(r.message.includes('re-muting (click #1)'), true, `[M56] and the watcher's own reason survives (verdict ${r.verdict})`);
  }
}

// 69. [M57] Repeat failures must read as a pattern. The count is prior + THIS
//     one, so a second proven-ineffective click reports 2, not 1 — dropping the
//     +1 makes this go red.
{
  const mk = (prior: number) =>
    reportZoomMuteClick({
      reason: 'r',
      before: clickedOn,
      after: readBackUnmuted,
      afterDetail: 'd',
      priorIneffectiveClicks: prior,
    }).message;
  assertEqual(mk(0).includes('(1 ineffective click(s) so far)'), true, '[M57] the first proven failure counts 1');
  assertEqual(mk(1).includes('(2 ineffective click(s) so far)'), true, '[M57] the second counts 2 — prior + this one');
  assertEqual(mk(2).includes('(3 ineffective click(s) so far)'), true, '[M57] and the third counts 3');
}

console.log('\n=== W6: source guards for the watcher click branch (Playwright) ===');

// 70. The watcher driver is setInterval + Playwright and is not executable by a
//     unit test, so these are SOURCE guards — weaker than behavioural tests and
//     not presented as equivalent. They pin the ORDER click -> reveal -> probe
//     -> report, which is the whole fix, and pin the removal of the false
//     success string.
{
  const src = readPrepareSource();
  const start = src.indexOf('export function startZoomMuteWatcher');
  assertEqual(start > -1, true, 'startZoomMuteWatcher is present in prepare.ts');
  const fn = src.slice(start, src.indexOf('\n}\n', start));

  assertEqual(
    fn.includes('Mute watcher re-muted the bot'),
    false,
    '[M58] the unconditional false-success line is GONE from the watcher',
  );
  assertEqual(fn.includes('const report = reportZoomMuteClick({'), true, '[M58] the click outcome goes through reportZoomMuteClick');
  // NOTE: this used to assert `after: verifyError ? null :
  // selectZoomMicToggle(afterCandidates),` — which CODIFIED a real defect (the
  // re-read was not pinned to the clicked element). Asserting it was true is
  // what made the bug look deliberate. The replacement lives in test 105 below;
  // all this one still needs to pin is that `after` is a re-read at all and not
  // the reading that triggered the click.
  assertEqual(fn.includes('after: verifyError'), true, '[M58] `after` is derived from the post-click probe, not from the trigger reading');
  assertEqual(fn.includes('after: selection,'), false, '[M58] and is never the trigger selection itself');
  assertEqual(fn.includes('describeZoomMicCandidates(poll ? poll.lastCandidates : [], rejected),'), true, '[M58] the digest comes from the LAST poll probe, annotated with the retirements');
  // A verification that dies must still produce a click report. Without its own
  // try/catch the outer handler logs a generic "pass failed" and the click goes
  // entirely unaccounted for — the same invisibility as Defect A.
  assertEqual(fn.includes('verifyError = ve?.message ?? String(ve);'), true, '[M58] a failed verification is captured, not thrown to the generic handler');
  assertEqual(fn.includes('`post-click verification failed: ${verifyError}`'), true, '[M58] and it is named in the report detail');
  assertEqual(
    fn.includes('log(report.warn ? `[Zoom Web] WARNING: ${report.message}` : `[Zoom Web] ${report.message}`);'),
    true,
    '[M58] the verdict is logged at the level the report chose',
  );
  assertEqual(fn.includes("if (report.verdict === 'still-unmuted') ineffectiveClicks++;"), true, '[M58] only a PROVEN failure is tallied');

  // Ordering. The re-probe must happen after the click and after the footer is
  // re-revealed; probing a hidden toolbar returns nothing (zero-area elements
  // are skipped) and would report every click as unverifiable.
  // REWRITTEN for the settle poll: the reveal+probe pair now lives inside the
  // prober CLOSURE handed to pollZoomMuteSettled, so it runs before EVERY poll
  // rather than once. The ordering that matters is therefore inside the closure.
  const clickIdx = fn.indexOf('.click({ timeout: 3000 });');
  const pollIdx = fn.indexOf('poll = await pollZoomMuteSettled(');
  const reportIdx = fn.indexOf('const report = reportZoomMuteClick({');
  assertEqual(clickIdx > -1 && pollIdx > -1 && reportIdx > -1, true, '[M58] click, settle poll and report are all present');
  assertEqual(clickIdx < pollIdx, true, '[M58] the settle poll runs AFTER the click');
  assertEqual(pollIdx < reportIdx, true, '[M58] and the report is built from the poll result');
  // The prober closure must reveal the footer before probing, on every poll —
  // probing a hidden toolbar returns nothing (zero-area elements are skipped)
  // and would report every click as unverifiable.
  const closure = fn.slice(pollIdx, fn.indexOf('selection.candidate.probe.elementKey,', pollIdx));
  assertEqual(closure.includes('await revealZoomFooter(page);'), true, '[M58] the prober closure reveals the footer');
  assertEqual(
    closure.indexOf('await revealZoomFooter(page);') < closure.indexOf('return probeZoomMicCandidates(page'),
    true,
    '[M58] and does so BEFORE probing, on every poll rather than once',
  );
  // The single fixed sample is gone. This literal is the defect itself.
  assertEqual(codeOnly(fn).includes('await page.waitForTimeout(750)'), false, '[M58] the single fixed 750ms sample is GONE');
}

console.log('\n=== W7: candidate discovery — rejecting an element that is not a toggle ===');

// The live element: `button.join-audio-container__btn`, aria-label="audio", no
// aria-pressed, state decided ONLY by the rank-6 substring over a descendant
// icon class. Two clicks landed on it and the reading did not move.
const liveProbeFields = {
  ariaLabel: 'audio',
  className: 'footer-button-base__button ax-outline join-audio-container__btn',
  descendantClassNames: ['SvgAudioUnmuted'],
  elementKey: 'div:2/div:0/button:0',
};

// W11: since the class-hint `unmuted` was demoted, `liveProbeFields` no longer
// yields an ACTIONABLE reading — which is the fix, and is asserted directly in
// the W11 section below. The candidate-discovery mechanism tests still need a
// first candidate the watcher would really click, so they use this variant: the
// same element with its unmuted glyph actually RENDERED, which reads `unmuted`
// on the precise whitelist tier. Keeping the two apart is deliberate — folding
// them together would have quietly turned every M59/M60 assertion into a test
// of a state the producer no longer emits.
const clickableProbeFields = {
  ...liveProbeFields,
  visibleDescendantClassNames: ['SvgAudioUnmuted'],
};

// 71. [M59] Two entries in zoomMicToggleSelectors match the SAME button, so a
//     rejection keyed on `selector[index]` would leave the identical element
//     selectable under the next selector name and the loop would re-click it
//     forever. Rejection is keyed on elementKey for exactly that reason — and
//     this is the assertion that fails if it goes back to a selector key.
{
  const sameElementTwice: ZoomMicCandidate[] = [
    { selector: 'button.join-audio-container__btn', index: 0, probe: probe(clickableProbeFields) },
    { selector: 'button[class*="join-audio-container" i]', index: 0, probe: probe(clickableProbeFields) },
  ];
  const first = selectZoomMicToggle(sameElementTwice);
  assertEqual(first !== null && first.candidate.selector, 'button.join-audio-container__btn', '[M59] with nothing rejected the first selector wins');

  const rejected = new Set<string>(['div:2/div:0/button:0']);
  assertEqual(
    selectZoomMicToggle(sameElementTwice, rejected),
    null,
    '[M59] rejecting the ELEMENT skips it under EVERY selector that matches it (no re-click loop)',
  );
}

// 72. [M60] …and the fall-through actually happens: a genuinely different
//     element further down the list is still selected after the first is
//     rejected. Without this, "rejection" could just be "stop working".
{
  const twoElements: ZoomMicCandidate[] = [
    { selector: 'button.join-audio-container__btn', index: 0, probe: probe(clickableProbeFields) },
    {
      selector: 'footer button[aria-label*="mute" i]',
      index: 1,
      probe: probe({ ariaLabel: 'Mute', elementKey: 'div:2/div:0/button:1' }),
    },
  ];
  const before = selectZoomMicToggle(twoElements);
  assertEqual(before !== null && before.candidate.index, 0, '[M60] the first candidate is chosen first');

  const after = selectZoomMicToggle(twoElements, new Set(['div:2/div:0/button:0']));
  assertEqual(after !== null && after.candidate.index, 1, '[M60] once rejected, the NEXT candidate is selected — discovery, not shutdown');
  assertEqual(after !== null && after.reading.kind, 'unmuted', '[M60] and it is read on its own evidence');
}

// 73. [M61] A probe with no identity must NOT be rejectable: an empty key in
//     the set would match every unidentified element and silence the watcher
//     wholesale. This is the guard for the "control accidentally equal to the
//     ambient value" shape — '' is the default elementKey.
{
  const noKey: ZoomMicCandidate[] = [{ selector: 's', index: 0, probe: probe({ ariaLabel: 'Mute' }) }];
  assertEqual(probe({}).elementKey, '', '[M61] an unidentified probe has an EMPTY key (the ambient value)');
  assertEqual(
    selectZoomMicToggle(noKey, new Set([''])) !== null,
    true,
    '[M61] an empty key in the rejection set does NOT suppress an unidentified candidate',
  );
}

// 74. [M62] The rejection POLICY lives in reportZoomMuteClick, so it is
//     testable without a browser: only a PROVEN-ineffective click rejects.
{
  const before: ZoomMicSelection = {
    candidate: { selector: 'button.join-audio-container__btn', index: 0, probe: probe(liveProbeFields) },
    reading: { kind: 'unmuted', evidence: 'class hint "svgaudiounmuted"' },
  };
  const mk = (after: ZoomMicSelection | null) =>
    reportZoomMuteClick({ reason: 'r', before, after, afterDetail: 'd', priorIneffectiveClicks: 0 });

  const stillUnmuted = mk({ candidate: before.candidate, reading: { kind: 'unmuted', evidence: 'aria-label "mute" offers mute' } });
  assertEqual(stillUnmuted.rejectKey, 'div:2/div:0/button:0', '[M62] a PROVEN-ineffective click rejects the element it clicked');
  assertEqual(stillUnmuted.message.includes('now REJECTED for the session (key=div:2/div:0/button:0)'), true, '[M62] and the transition is logged with the key');
  assertEqual(stillUnmuted.message.includes('falls through to the next candidate'), true, '[M62] saying what happens next');

  const confirmed = mk({ candidate: before.candidate, reading: { kind: 'muted', evidence: 'class hint "svgaudiomuted"' } });
  assertEqual(confirmed.rejectKey, null, '[M62] a click that WORKED never rejects the element that worked');

  const unreadable = mk(null);
  assertEqual(unreadable.rejectKey, null, '[M62] an UNVERIFIABLE click does not reject — unknown is not proven failure');
  assertEqual(unreadable.message.includes('the candidate is NOT rejected'), true, '[M62] and the log says so, so a reader does not assume otherwise');
}

// 75. [M63] An identity-less candidate cannot be rejected, and the log must say
//     that the next pass may pick it again rather than implying it was retired.
{
  const before: ZoomMicSelection = {
    candidate: { selector: 's', index: 0, probe: probe({ ariaLabel: 'Mute' }) },
    reading: { kind: 'unmuted', evidence: 'aria-label "mute" offers mute' },
  };
  const r = reportZoomMuteClick({
    reason: 'r',
    before,
    after: { candidate: before.candidate, reading: { kind: 'unmuted', evidence: 'aria-label "mute" offers mute' } },
    afterDetail: 'd',
    priorIneffectiveClicks: 0,
  });
  assertEqual(r.verdict, 'still-unmuted', '[M63] the verdict is still a proven failure');
  assertEqual(r.rejectKey, null, '[M63] but an EMPTY key is never handed to the rejection set');
  assertEqual(r.message.includes('could NOT be rejected'), true, '[M63] and the log admits the candidate may be picked again');
}

// 76. The digest marks rejected candidates, so a blind pass is diagnosable:
//     "no readable control" and "every readable control proven wrong" are
//     different problems and only one of them means the selectors need work.
{
  const cands: ZoomMicCandidate[] = [{ selector: 'button.x', index: 0, probe: probe(liveProbeFields) }];
  assertEqual(describeZoomMicCandidates(cands).includes('REJECTED'), false, 'digest does not mark an un-rejected candidate');
  assertEqual(
    describeZoomMicCandidates(cands, new Set(['div:2/div:0/button:0'])).includes('REJECTED'),
    true,
    'digest marks a rejected candidate',
  );
  assertEqual(describeZoomMicCandidates(cands).includes('key=div:2/div:0/button:0'), true, 'digest reports the element identity');
}

console.log('\n=== W8: structural discriminators — visible text ===');

// 77. [M64] THE ONE THAT WOULD HAVE CAUGHT THE LIVE FAILURE. aria-label="audio"
//     carries no mute vocabulary; the label NODE carries the word the room
//     actually reads. With text present the reading no longer rests on a
//     substring presence test over a class attribute.
{
  const joined = readZoomMicState(probe({ ...liveProbeFields, labelText: 'Mute' }));
  assertEqual(joined.kind, 'unmuted', '[M64] label node "Mute" => unmuted');
  assertEqual(joined.evidence.includes('label node "mute" offers mute'), true, '[M64] and the evidence names the label node, not a class hint');

  const muted = readZoomMicState(probe({ ...liveProbeFields, labelText: 'Unmute' }));
  assertEqual(muted.kind, 'muted', '[M64] label node "Unmute" => muted (unmute tested before mute)');
  assertEqual(muted.evidence.includes('offers unmute'), true, '[M64] with the right polarity');
}

// 78. [M65] "Join Audio" positively identifies the UNJOINED control. No class
//     substring can do this, and it is the reading that must never be clicked.
{
  const r = readZoomMicState(probe({ ariaLabel: 'audio', labelText: 'Join Audio' }));
  assertEqual(r.kind, 'not-mute-toggle', '[M65] label node "Join Audio" => not-mute-toggle');
  assertEqual(r.evidence.includes('UNJOINED audio control'), true, '[M65] named explicitly, not lumped in with "unreadable"');
  assertEqual(selectZoomMicToggle([{ selector: 's', index: 0, probe: probe({ labelText: 'Join Audio' }) }]), null, '[M65] and it is never selected for a click');
}

// 79. [M66] The label NODE outranks whole-element textContent: a wrapper's text
//     can concatenate several buttons' words, and the label node is the precise
//     one. Fixture makes them DISAGREE so the winner is unambiguous — with both
//     saying the same thing this test would pass for either ranking.
{
  const r = readZoomMicState(probe({ labelText: 'Unmute', text: 'Mute Participants Chat' }));
  assertEqual(r.kind, 'muted', '[M66] the label node wins over whole-element text');
  assertEqual(r.evidence.includes('label node'), true, '[M66] and the evidence says which source decided');

  const textOnly = readZoomMicState(probe({ text: 'Unmute' }));
  assertEqual(textOnly.kind, 'muted', '[M66] with no label node, textContent is used');
  assertEqual(textOnly.evidence.includes('text "unmute"'), true, '[M66] and is named as the weaker source');
}

// 80. [M67] aria-label mute vocabulary still outranks text. Fixture makes them
//     CONTRADICT: aria-label is an explicit accessibility contract and is
//     live-confirmed on this control in preview, so it stays rank 1.
{
  const r = readZoomMicState(probe({ ariaLabel: 'Unmute', labelText: 'Mute' }));
  assertEqual(r.kind, 'muted', '[M67] aria-label outranks the label node when they disagree');
  assertEqual(r.evidence.includes('aria-label'), true, '[M67] and the evidence names aria-label');
}

// 81. [M68] The non-mic guard now covers TEXT too. "Mute All" as a text label
//     must not read as unmuted — clicking it would mute every human in the
//     meeting, which is the worst outcome in this whole file.
{
  assertEqual(readZoomMicState(probe({ labelText: 'Mute All' })).kind, 'not-mic-control', '[M68] "Mute All" via the label node => not-mic-control (never clicked)');
  assertEqual(readZoomMicState(probe({ text: 'Mute All' })).kind, 'not-mic-control', '[M68] "Mute All" via textContent => not-mic-control (never clicked)');
  const askToUnmute = readZoomMicState(probe({ labelText: 'Ask to Unmute' }));
  assertEqual(askToUnmute.kind, 'not-mic-control', '[M68] "Ask to Unmute" as text is not a mic control either');
}

console.log('\n=== W8: structural discriminators — icon whitelist over VISIBLE nodes ===');

// 82. [M69] Exact full-token match on a visibly rendered descendant, ranked
//     ABOVE the substring tier. The evidence distinguishes the two tiers, which
//     is how a reader tells a precise reading from a guess in the log.
{
  const r = readZoomMicState(probe({ visibleDescendantClassNames: ['zm-icon SvgAudioUnmuted'] }));
  assertEqual(r.kind, 'unmuted', '[M69] visible icon token "svgaudiounmuted" => unmuted');
  assertEqual(r.evidence.includes('visible icon class "svgaudiounmuted"'), true, '[M69] evidence names the WHITELIST tier');
  assertEqual(r.evidence.includes('class hint'), false, '[M69] and not the substring tier');

  const m = readZoomMicState(probe({ visibleDescendantClassNames: ['SvgAudioMuted'] }));
  assertEqual(m.kind, 'muted', '[M69] visible icon token "svgaudiomuted" => muted');
}

// 83. [M70] A HEADSET/join glyph is the unjoined control, not a mic state — the
//     distinction that full-name matching buys and substring matching cannot.
{
  const r = readZoomMicState(probe({ visibleDescendantClassNames: ['SvgJoinAudio'] }));
  assertEqual(r.kind, 'not-mute-toggle', '[M70] a join-audio glyph is not a mute toggle');
  assertEqual(r.evidence.includes('UNJOINED audio control'), true, '[M70] and says so');
}

// 84. [M71] The whitelist ranks ABOVE the substring hints. Fixture makes them
//     CONTRADICT — visible icon says muted, hidden descendant says unmuted — so
//     whichever wins is unambiguous. Under the old code the unmuted substring
//     would win and the bot would click a control that is already muted.
{
  const r = readZoomMicState(probe({
    descendantClassNames: ['SvgAudioUnmuted'],
    visibleDescendantClassNames: ['SvgAudioMuted'],
  }));
  assertEqual(r.kind, 'muted', '[M71] the VISIBLE whitelist beats a hidden substring hint');
  assertEqual(r.evidence.includes('visible icon class'), true, '[M71] and the evidence proves which tier decided');
}

// 85. [M72] The substring tier is KEPT, not replaced: its bare-word entries are
//     what survive Zoom renaming a glyph (mutation M3), and a whitelist-only
//     reader goes blind on the next rename. An unknown-but-plausible class must
//     still read, at the lower confidence the evidence declares.
{
  const r = readZoomMicState(probe({ descendantClassNames: ['SvgSomethingNewlyRenamedUnmuted'] }));
  assertEqual(r.kind, 'unmuted-unconfirmed', '[M72] a renamed glyph still reads a DIRECTION via the substring fallback');
  assertEqual(r.evidence.includes('class hint'), true, '[M72] and the evidence marks it as the WEAK tier');
  // The muted direction is NOT demoted — it only ever declines to act, so the
  // rename resilience M72 exists to protect is fully intact there.
  const m = readZoomMicState(probe({ descendantClassNames: ['SvgSomethingNewlyRenamedMuted'] }));
  assertEqual(m.kind, 'muted', '[M72] and a renamed MUTED glyph still reads muted — the safe direction keeps full strength');
}

console.log('\n=== W8: structural discriminators — the caret is REPORTED, never gated ===');

// 86. [M73] zoomMicCaretSelectors is an unverified guess. If a reading were
//     vetoed on caret absence and the selectors never match the real caret, the
//     watcher goes permanently blind — the exact failure M30 fixed. So the caret
//     appears in the evidence and changes NO reading. These assertions are what
//     fail if someone promotes it to a discriminator without a live run.
{
  const yes = readZoomMicState(probe({ ariaLabel: 'Mute', caretNearby: true }));
  const no = readZoomMicState(probe({ ariaLabel: 'Mute', caretNearby: false }));
  const unprobed = readZoomMicState(probe({ ariaLabel: 'Mute' }));
  assertEqual(yes.kind, 'unmuted', '[M73] caret present: reading unchanged');
  assertEqual(no.kind, 'unmuted', '[M73] caret ABSENT: reading unchanged — no veto');
  assertEqual(unprobed.kind, 'unmuted', '[M73] caret not probed: reading unchanged');
  assertEqual(yes.evidence.includes('[caret: yes]'), true, '[M73] but presence is reported');
  assertEqual(no.evidence.includes('[caret: no]'), true, '[M73] and so is absence');
  assertEqual(unprobed.evidence.includes('caret'), false, '[M73] while "not probed" claims nothing at all');
  // The same must hold for the weakest tier, which is where a veto would bite
  // hardest: the live 2026-09-02 reading came from a class hint with no caret
  // information at all.
  const hintNoCaret = readZoomMicState(probe({ descendantClassNames: ['SvgAudioMuted'], caretNearby: false }));
  assertEqual(hintNoCaret.kind, 'muted', '[M73] a class-hint reading is not vetoed by caret absence either');
}

console.log('\n=== W8: source guards for the in-page probe (page.evaluate) ===');

// 87. probeZoomMicCandidates runs inside page.evaluate and cannot be executed
//     here. SOURCE guards only — weaker than behavioural tests, not presented
//     as equivalent. They pin the properties the pure reader cannot see: what
//     the probe actually harvests, and that identity excludes the class.
{
  const src = readPrepareSource();
  const start = src.indexOf('async function probeZoomMicCandidates');
  assertEqual(start > -1, true, 'probeZoomMicCandidates is present in prepare.ts');
  const fn = src.slice(start, src.indexOf('\n}\n', start));

  assertEqual(fn.includes('labelText = renderedTextOf(labelNode)'), true, '[M74] the probe harvests the label node text');
  assertEqual(fn.includes('text: renderedTextOf(el),'), true, '[M74] and the element text as a fallback');
  // W5: textContent includes display:none subtrees, and the text tier outranks
  // the icon whitelist AND feeds the non-mic guard — so hidden text must not
  // reach it. `el.textContent` must not be the source for either text field.
  assertEqual(codeOnly(fn).includes('el.textContent'), false, '[M74] raw textContent is NOT used in CODE — it would include display:none subtrees');
  assertEqual(fn.includes('if (!isRendered(el)) continue; // display:none / zero-area subtree'), true, '[M74] renderedTextOf skips unrendered subtrees');
  assertEqual(fn.includes('.filter(isRendered)'), true, '[M74] visibleDescendantClassNames is filtered to RENDERED nodes only');
  // W10: filter THEN slice. The reverse order starved the precise tier whenever
  // a button's first N [class] descendants were hidden.
  const filterIdx = fn.indexOf('.filter(isRendered)');
  const sliceIdx = fn.indexOf('.slice(0, MAX_DESCENDANTS)', filterIdx);
  assertEqual(filterIdx > -1 && sliceIdx > filterIdx, true, '[M74] the RENDERED filter runs BEFORE the slice, not after');
  assertEqual(fn.includes('MAX_DESCENDANT_SCAN'), true, '[M74] and more descendants are scanned than kept, so the filter has a choice');
  assertEqual(fn.includes('caretNearby = true;'), true, '[M74] the split-button caret is harvested');
  assertEqual(fn.includes('elementKey: identify(el),'), true, '[M74] every candidate carries an element identity');
  // Identity must be state-INDEPENDENT. If the class attribute went into the
  // key, the key would change the moment the mic state changed and a rejection
  // would silently stop applying to the element it was created for.
  const identStart = fn.indexOf('const identify =');
  const identBody = fn.slice(identStart, fn.indexOf('};', identStart));
  assertEqual(identStart > -1, true, '[M75] the identity helper is present');
  assertEqual(identBody.includes("getAttribute('id')"), true, '[M75] identity prefers a stable id');
  assertEqual(identBody.includes('tagName.toLowerCase()'), true, '[M75] and falls back to a tag/child-index path');
  assertEqual(identBody.includes("getAttribute('class')"), false, '[M75] identity NEVER includes the class attribute (it changes with the state)');
  assertEqual(identBody.includes('aria-label'), false, '[M75] nor aria-label (it changes with the state too)');
}

// 88. The watcher must thread the rejection set into BOTH the selection and the
//     digest, and must retire what the report rejected. setInterval + Playwright,
//     so source guards again.
{
  const src = readPrepareSource();
  const start = src.indexOf('export function startZoomMuteWatcher');
  const fn = src.slice(start, src.indexOf('\n}\n', start));
  assertEqual(fn.includes('const rejected = new Set<string>();'), true, '[M76] the watcher keeps a session-scoped rejection set');
  assertEqual(fn.includes('selectZoomMicToggle(candidates, rejected)'), true, '[M76] and passes it to the selection, so a retired element is skipped');
  // REWRITTEN: retirement is no longer an unconditional `rejected.add`. A
  // proven failure is a STRIKE and only the second strike on the same key
  // retires; `rejected` is kept in step with the retirement state.
  assertEqual(fn.includes('const rs = stepZoomRetirement(retirement, report.rejectKey);'), true, '[M76] a proven failure goes through the corroborating retirement machine');
  assertEqual(codeOnly(fn).includes('rejected.add(report.rejectKey)'), false, '[M76] and is NEVER retired straight from one report');
  assertEqual(fn.includes('syncRejected();'), true, '[M76] the selection set is kept in step with the retirement state');
  assertEqual(fn.includes('if (zoomRetirementIsAbsorbing(candidates, rejected)) {'), true, '[M76] and "every readable control retired" is detected');
  assertEqual(fn.includes('retirement = resetZoomRetirement(retirement);'), true, '[M76] and cleared, so it is not an absorbing state');
  assertEqual(fn.includes('rejected.size'), true, '[M76] a blind pass reports how many candidates have been rejected');
  // Order: the set must be consulted by the selection BEFORE the click, or the
  // retirement has no effect on what gets clicked next.
  assertEqual(
    fn.indexOf('selectZoomMicToggle(candidates, rejected)') < fn.indexOf('.click({ timeout: 3000 })'),
    true,
    '[M76] the rejection set is consulted BEFORE the click, not after',
  );
}

console.log('\n=== W9: ZOOM_AUDIO_LOCK=none — a genuine hands-off mode ===');

// 89. [M77] The three modes. 'off' must keep parsing EXACTLY as before (its
//     behaviour is already reasoned about and deployed), so the mode parser
//     recognises hands-off first and delegates everything else.
{
  assertEqual(parseZoomAudioLockMode(undefined), 'on', '[M77] DEFAULT: unset => on');
  assertEqual(parseZoomAudioLockMode(''), 'on', '[M77] empty => on');
  for (const off of ['off', '0', 'false', 'no', 'disabled', 'OFF', '  Off  ']) {
    assertEqual(parseZoomAudioLockMode(off), 'off', `[M77] "${off}" => off (unchanged from the boolean switch)`);
  }
  for (const none of ['none', 'NONE', '  none  ', 'hands-off', 'handsoff', 'no-touch', 'notouch']) {
    assertEqual(parseZoomAudioLockMode(none), 'none', `[M77] "${none}" => none`);
  }
  // A typo must not silently disarm anything — same asymmetry the boolean uses.
  for (const typo of ['nope', 'nonetheless', 'on', 'yes', 'enabled']) {
    assertEqual(parseZoomAudioLockMode(typo), 'on', `[M77] unrecognised "${typo}" => on (a typo must not disarm)`);
  }
}

// 90. [M78] THE TRAP, asserted rather than left in a comment: the OLD boolean
//     parser reads 'none' as seal-ENABLED, because 'none' is not in its opt-out
//     list. Any call site that consults the boolean to decide whether to TOUCH a
//     track would therefore touch tracks in hands-off mode. This test exists so
//     that trap is a documented, checked fact and not a latent surprise.
{
  assertEqual(parseZoomAudioLockEnv('none'), true, '[M78] the old boolean parser reads "none" as ENABLED — it must not gate track-touching');
  assertEqual(parseZoomAudioLockMode('none'), 'none', '[M78] only the MODE parser sees hands-off');
}

// 91. [M79] 'off' and 'none' are genuinely different, which is the whole point:
//     'off' still writes track.enabled = false (lockTrack does that BEFORE
//     consulting the switch), so it cannot isolate a Zoom audio-join failure
//     caused by the WRITE rather than by the unreadable getter.
{
  assertEqual(parseZoomAudioLockMode('off') === parseZoomAudioLockMode('none'), false, '[M79] off and none are distinct modes');

  // The 'off' behaviour itself, unchanged: seal absent, but the track IS still
  // disabled. This is the property that makes 'off' insufficient as an
  // isolation tool, and it is pinned here so a future edit cannot quietly
  // "improve" one of the two modes into the other.
  const g = globalThis as unknown as Record<string, unknown>;
  const savedLock = Object.getOwnPropertyDescriptor(g, '__vexa_zoom_audio_lock');
  delete g.__vexa_zoom_audio_lock;
  try {
    const t = makeTrack('audio');
    const pc = { getSenders: () => [{ track: t.track }] };
    (g as Record<string, unknown>).__vexa_zoom_peer_connections = [pc];
    installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: false });
    assertEqual(t.real(), false, '[M79] mode off: the track is STILL written enabled=false');
    t.track.enabled = true;
    assertEqual(t.real(), true, '[M79] mode off: and the unmute SUCCEEDS (no seal) — so off is not hands-off');
  } finally {
    delete g.__vexa_zoom_peer_connections;
    delete g.__vexa_zoom_audio_lock;
    if (savedLock) Object.defineProperty(g, '__vexa_zoom_audio_lock', savedLock);
  }
}

console.log('\n=== W9: source guards for the hands-off gating (env + Playwright) ===');

// 92. Hands-off must be enforced at EVERY site that installs a patch or writes
//     to a track. These are source guards over three files: the env read is
//     process-global and memoised, so a behavioural test here would leak state
//     across the whole suite. Weaker than behavioural proof, and not offered as
//     equivalent — each assertion names the edit it catches.
{
  const src = readPrepareSource();
  const touchStart = src.indexOf('export function isZoomAudioTouchAllowed');
  assertEqual(touchStart > -1, true, '[M80] isZoomAudioTouchAllowed exists — one predicate, not a per-caller check');
  const touchFn = src.slice(touchStart, src.indexOf('\n}\n', touchStart));
  assertEqual(touchFn.includes("zoomAudioLockMode() !== 'none'"), true, '[M80] and it is false ONLY in hands-off mode');
  assertEqual(touchFn.includes('parseZoomAudioLockEnv'), false, '[M80] never via the boolean parser, which misreads "none" as enabled');

  // join.ts: the page-load arm must not even be REGISTERED in hands-off mode.
  const joinSrc = readZoomWebSource('join.ts');
  assertEqual(joinSrc.includes('} else if (!isZoomAudioTouchAllowed()) {'), true, '[M81] join.ts gates the page-load arm on hands-off');
  assertEqual(
    joinSrc.indexOf('} else if (!isZoomAudioTouchAllowed()) {') < joinSrc.indexOf('await page.addInitScript(installOutboundAudioLockInPage'),
    true,
    '[M81] and the gate precedes the addInitScript, so nothing is installed rather than installed-then-returning',
  );

  // index.ts: steps 1-3 gated; step 4 + the DOM mute deliberately NOT gated.
  const indexSrc = readZoomWebSource('index.ts');
  assertEqual(indexSrc.includes('const touchAllowed = isZoomAudioTouchAllowed();'), true, '[M82] index.ts reads the predicate once');
  assertEqual(indexSrc.includes('if (!voiceAgentEnabled && touchAllowed) {'), true, '[M82] and gates the track-touching steps on it');
  const sweepIdx = indexSrc.indexOf('await sweepZoomOutboundAudio(page, false)');
  const lockIdx = indexSrc.indexOf('await installZoomOutboundAudioLock(page, voiceAgentEnabled)');
  const guardIdx = indexSrc.indexOf('startZoomOutboundAudioGuard(page, false)');
  const gateIdx = indexSrc.indexOf('if (!voiceAgentEnabled && touchAllowed) {');
  const domGateIdx = indexSrc.lastIndexOf('if (!voiceAgentEnabled) {');
  const watcherIdx = indexSrc.indexOf('startZoomMuteWatcher(page, false)');
  const domMuteIdx = indexSrc.indexOf('void ensureZoomMutedInMeeting(page)');
  // CONTAINMENT, not ordering. Suggestion 1: the previous form was
  // `gateIdx < sweepIdx`, which a move below the closing brace keeps green.
  const gateBlock = extractBraceBlock(indexSrc, gateIdx);
  assertEqual(gateBlock !== null, true, '[M82] the track-touching gate block is balanced and extractable');
  assertEqual(
    gateBlock !== null && gateBlock.includes('await sweepZoomOutboundAudio(page, false)'),
    true,
    '[M82] the sweep is INSIDE the gate block (brace-matched, not merely after it)',
  );
  assertEqual(
    gateBlock !== null && gateBlock.includes('await installZoomOutboundAudioLock(page, voiceAgentEnabled)'),
    true,
    '[M82] the lock install is inside it',
  );
  assertEqual(
    gateBlock !== null && gateBlock.includes('startZoomOutboundAudioGuard(page, false)'),
    true,
    '[M82] and the guard is inside it',
  );
  // Positive control for the matcher itself: it must NOT swallow the DOM half,
  // which lives outside the gate. Without this, a matcher that ran to the end of
  // the file would satisfy every assertion above.
  assertEqual(
    gateBlock !== null && gateBlock.includes('void ensureZoomMutedInMeeting(page)'),
    false,
    '[M82] CONTROL: the extracted block STOPS at the gate — the DOM half is not inside it',
  );
  assertEqual(extractBraceBlock('no braces here', 0), null, '[M82] CONTROL: the matcher returns null when there is nothing to match');
  assertEqual(extractBraceBlock('{ unbalanced', 0), null, '[M82] CONTROL: and when the block never closes');
  assertEqual(extractBraceBlock('x { a { b } c } y', 0), '{ a { b } c }', '[M82] CONTROL: and it spans nested braces');
  // The DOM half must sit in a LATER block that is gated only on voiceAgent —
  // in hands-off mode it is the only thing maintaining the visual state.
  assertEqual(
    domGateIdx > guardIdx && domGateIdx < watcherIdx && domGateIdx < domMuteIdx,
    true,
    '[M83] the mute watcher and the DOM mute are in a separate block AFTER the gate, so hands-off still runs them',
  );
  assertEqual(domGateIdx > gateIdx, true, '[M83] and that block is not the track-touching gate itself');
  // ORDERING ALONE IS NOT ENOUGH — proven: a mutation that wrapped the DOM
  // block in `if (touchAllowed) {` left every index above unchanged and
  // SURVIVED. So assert the gate is CLOSED before the DOM block starts, and
  // that nothing re-gates in between.
  const betweenGateAndDom = indexSrc.slice(guardIdx, domGateIdx);
  assertEqual(
    betweenGateAndDom.includes('\n      }\n'),
    true,
    '[M83] the track-touching gate is CLOSED before the DOM block opens (not merely earlier in the file)',
  );
  assertEqual(
    betweenGateAndDom.includes('touchAllowed'),
    false,
    '[M83] and nothing re-gates the DOM half on touchAllowed in between',
  );
  assertEqual(
    indexSrc.slice(domGateIdx).includes('touchAllowed'),
    false,
    '[M83] nor anywhere after it — hands-off must still reach the watcher and the DOM mute',
  );

  // The guard checks the mode itself as well, so 'none' is a property of the
  // machinery rather than of one caller that could be bypassed.
  const guardStart = src.indexOf('export function startZoomOutboundAudioGuard');
  const guardFn = src.slice(guardStart, src.indexOf('\n}\n', guardStart));
  assertEqual(guardFn.includes('if (!isZoomAudioTouchAllowed()) {'), true, '[M84] the guard refuses to arm in hands-off mode on its own account');
  assertEqual(
    guardFn.indexOf('if (!isZoomAudioTouchAllowed()) {') < guardFn.indexOf('setInterval'),
    true,
    '[M84] and refuses BEFORE the interval is created',
  );
}

console.log('\n=== W10: start.sh PulseAudio topology (shell — static assertions only) ===');

/**
 * Locate the zoom-bot start.sh. It is a SHELL SCRIPT: no unit test in this
 * harness can execute it, and nothing here simulates PulseAudio. These are
 * static assertions on the script text — strictly weaker than behavioural
 * proof and NOT offered as equivalent. They exist because the single most
 * dangerous mistake in this change (deriving the microphone from the RECORDING
 * sink's monitor, which would let the bot transmit the meeting back into the
 * meeting) is a one-word edit, and a one-word edit deserves a tripwire.
 *
 * Fails loudly if the file cannot be found — a guard that silently stops
 * running is worse than no guard.
 */
function readZoomBotStartScript(): string {
  const candidates = [
    '../../../aw-overrides/services/zoom-bot/start.sh',
    '../../../../aw-overrides/services/zoom-bot/start.sh',
    'aw-overrides/services/zoom-bot/start.sh',
    'vexa-fork/aw-overrides/services/zoom-bot/start.sh',
    'services/vexa-bot/core/../../../aw-overrides/services/zoom-bot/start.sh',
  ];
  for (const rel of candidates) {
    // NEVER read a frozen copy. docs-utpal/change-snapshots/ holds per-version
    // snapshots of this exact file, including an 82-line pre-fix one. Reading a
    // snapshot would make every assertion below describe a file nobody runs —
    // and it already caused a real "the fix is not in the tree" report when a
    // reviewer grepped the snapshot path instead of the live one.
    if (rel.includes('change-snapshots')) continue;
    try {
      return readFileSync(rel, 'utf8');
    } catch {
      /* try the next candidate */
    }
  }
  throw new Error(`could not locate zoom-bot start.sh from cwd=${process.cwd()} (tried: ${candidates.join(', ')})`);
}

/**
 * Collect `NAME="value"` assignments from a shell script (non-comment lines).
 *
 * WHY THIS EXISTS: the echo-loop guard matched the LITERAL
 * `master="${MIC_SILENCE_SINK}.monitor"` and therefore could not see a one-word
 * reintroduction of the loop via ALIASING — `MIC_SILENCE_SINK="$PULSE_SINK"`
 * keeps the literal identical while pointing the microphone at the recording
 * sink's monitor. That mutation SURVIVED the suite. Resolving the variable and
 * asserting the resolved value closes that whole family rather than one
 * spelling.
 *
 * HONEST LIMIT — THIS IS POSITION-BLIND. It walks every line and lets the LAST
 * assignment win, while bash uses whatever was in force ABOVE the line being
 * audited. So an alias in force AT the pactl line and reset AFTERWARDS resolves
 * to the innocent value here while bash uses the aliased one. That mutation
 * survived too: the aliasing class did not go away, it RELOCATED one level up —
 * round 1 the guard never read the definition, round 2 it reads definitions but
 * not their position.
 *
 * Rather than make the resolver position-aware, test M126 asserts the
 * PRECONDITION under which position-blindness is sound: each audited name is
 * assigned EXACTLY ONCE in the executed code, and then last-wins IS the value
 * in force at every line. If that ever stops holding, M126 fails and says why.
 */
/** Strip `#` comment lines, for NEGATIVE assertions on shell sources. */
function codeOnly2(sh: string): string {
  return sh.split('\n').filter((l) => !l.trimStart().startsWith('#')).join('\n');
}

function shellAssignments(sh: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of sh.split('\n')) {
    const t = line.trim();
    if (t.startsWith('#')) continue;
    const m = /^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)="([^"]*)"/.exec(t);
    if (!m) continue;
    let value = m[2];
    // The `NAME="${NAME:-default}"` idiom: use the default, or resolution loops.
    const selfDefault = new RegExp('^\\$\\{' + m[1] + ':-([^}]*)\\}$').exec(value);
    if (selfDefault) value = selfDefault[1];
    out[m[1]] = value;
  }
  return out;
}

/** Expand `$NAME` / `${NAME}` using the collected assignments. */
function resolveShell(value: string, vars: Record<string, string>, depth = 0): string {
  if (depth > 6) return value;
  const next = value.replace(
    /\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)/g,
    (whole, braced: string | undefined, bare: string | undefined) => {
      const name = braced ?? bare ?? '';
      return vars[name] !== undefined ? vars[name] : whole;
    },
  );
  return next === value ? value : resolveShell(next, vars, depth + 1);
}

// 93. [M85] THE ROOT CAUSE. The script created a null SINK and no capture
//     SOURCE, so Chromium had no microphone, Zoom never joined audio for
//     sending, audioSenders was 0 all meeting, and there was no audio session
//     for a mute state to describe.
{
  const sh = readZoomBotStartScript();
  assertEqual(sh.includes('module-remap-source'), true, '[M85] start.sh creates a remap SOURCE (a capture device Chromium will use as a mic)');
  assertEqual(sh.includes('pactl set-default-source "$BOT_MIC_SOURCE"'), true, '[M85] and makes it the default source');
}

// 94. [M86] THE FEEDBACK-LOOP TRIPWIRE — the most important assertion in this
//     file. ${PULSE_SINK}.monitor carries the meeting audio (parecord records
//     it). A microphone derived from it would let the bot transmit the entire
//     meeting back into the meeting. The mic's master must be the DEDICATED
//     silent sink's monitor, and must never be the recording sink's.
{
  const sh = readZoomBotStartScript();
  assertEqual(
    sh.includes('module-remap-source master="${MIC_SILENCE_SINK}.monitor"'),
    true,
    '[M86] the mic source master IS the dedicated silent sink monitor',
  );
  // Every spelling of the forbidden master, checked on the same line as the
  // remap so a mention inside the explanatory comment cannot satisfy it.
  const remapLines = sh.split('\n').filter((l) => l.includes('module-remap-source') && !l.trimStart().startsWith('#'));
  assertEqual(remapLines.length, 1, '[M86] exactly one remap-source line (a second one could reintroduce the loop)');
  for (const forbidden of ['${PULSE_SINK}.monitor', 'zoom_sink.monitor', '$PULSE_SINK.monitor']) {
    assertEqual(
      remapLines[0].includes(forbidden),
      false,
      `[M86] the mic is NOT derived from ${forbidden} — that is the meeting audio (echo loop)`,
    );
  }

  // RESOLVED, not literal. A literal-only guard misses aliasing:
  // `MIC_SILENCE_SINK="$PULSE_SINK"` keeps the text identical and points the mic
  // at the recording sink's monitor. That mutation survived until this block.
  const vars = shellAssignments(sh);
  assertEqual(vars.PULSE_SINK, 'zoom_sink', '[M86] FIXTURE: the resolver reads PULSE_SINK through the ${NAME:-default} idiom');
  const masterMatch = /master="([^"]*)"/.exec(remapLines[0]);
  assertEqual(masterMatch !== null, true, '[M86] the remap line has a master= argument');
  const resolvedMaster = masterMatch ? resolveShell(masterMatch[1], vars) : '';
  assertEqual(resolvedMaster, 'mic_silence_sink.monitor', '[M86] the RESOLVED mic master is the dedicated silent sink monitor');
  assertEqual(
    resolvedMaster === `${vars.PULSE_SINK}.monitor`,
    false,
    '[M86] and RESOLVES to something other than the recording sink monitor (closes the aliasing route)',
  );
  assertEqual(
    resolveShell('${MIC_SILENCE_SINK}', vars) === resolveShell('${PULSE_SINK}', vars),
    false,
    '[M86] the two sinks are genuinely different after resolution, not just differently spelled',
  );
  // Positive control: the resolver must actually be capable of catching it.
  const aliased = shellAssignments(sh.replace('MIC_SILENCE_SINK="mic_silence_sink"', 'MIC_SILENCE_SINK="$PULSE_SINK"'));
  assertEqual(
    resolveShell('${MIC_SILENCE_SINK}.monitor', aliased),
    'zoom_sink.monitor',
    '[M86] CONTROL: an aliased silent sink RESOLVES to the recording monitor, so the assertions above can fail',
  );
}

// 95. [M87] The silent sink must never become the default sink: Zoom would
//     render the meeting into it and the recording would be empty. And the
//     recording sink's default-sink assignment must come AFTER every sink is
//     loaded, so module-switch-on-connect (present in some PulseAudio configs)
//     cannot leave the default pointing at the newly created silent sink.
{
  const sh = readZoomBotStartScript();
  assertEqual(sh.includes('set-default-sink "$MIC_SILENCE_SINK"'), false, '[M87] the silent mic sink is NEVER made the default sink');
  const micSinkIdx = sh.indexOf('sink_name="$MIC_SILENCE_SINK"');
  const defaultSinkIdx = sh.lastIndexOf('pactl set-default-sink "$PULSE_SINK"');
  assertEqual(micSinkIdx > -1 && defaultSinkIdx > -1, true, '[M87] both the silent sink load and the default-sink assignment are present');
  assertEqual(
    defaultSinkIdx > micSinkIdx,
    true,
    '[M87] the recording sink is made default AFTER the silent sink is loaded (switch-on-connect cannot steal it)',
  );
}

// 96. [M88] The source mute must be in force BEFORE node launches: the interim
//     live run uses ZOOM_AUDIO_LOCK=off/none, which makes the PulseAudio source
//     mute the only hard guarantee, and the very first getUserMedia must
//     receive digital zeros. Muting the sink alone is documented as insufficient
//     in entrypoint.sh ("the remap source still passes a low-level signal").
{
  const sh = readZoomBotStartScript();
  const srcMuteIdx = sh.indexOf('pactl set-source-mute "$BOT_MIC_SOURCE" 1');
  const nodeIdx = sh.indexOf('node /app/dist/docker.js');
  assertEqual(srcMuteIdx > -1, true, '[M88] the SOURCE is muted (not just the sink)');
  assertEqual(nodeIdx > -1, true, '[M88] the node launch is present');
  assertEqual(srcMuteIdx < nodeIdx, true, '[M88] and the source mute happens BEFORE node starts');
  assertEqual(sh.includes('pactl set-sink-mute "$MIC_SILENCE_SINK" 1'), true, '[M88] the feed sink is muted too (defence in depth)');
}

// 96b. [M125] RESOLVED-VALUE AUDIT of every load-bearing pactl argument.
//       The lead's instruction after the echo-loop survivor: any assertion that
//       matches a ${VAR} SPELLING rather than a resolved VALUE has the identical
//       hole — the alias enters at the point of DEFINITION, a different line
//       that a text guard never reads. So resolve every argument that decides
//       where audio goes, and assert the resolved value.
{
  const sh = readZoomBotStartScript();
  const vars = shellAssignments(sh);
  const code = codeOnly2(sh);
  const resolvedPulse = resolveShell('${PULSE_SINK}', vars);
  const resolvedMicSink = resolveShell('${MIC_SILENCE_SINK}', vars);
  const resolvedMicSrc = resolveShell('${BOT_MIC_SOURCE}', vars);

  // [M126] SINGLE ASSIGNMENT — the precondition that makes resolveShell's
  // position-blindness sound. resolveShell lets the LAST assignment win; bash
  // uses whatever was in force ABOVE the line being audited. If each name is
  // assigned exactly once in the executed code those are the same thing. If it
  // is not, every resolved-value assertion below describes a value bash never
  // used at the pactl line — which is exactly how an alias in force at the
  // remap and reset afterwards slipped through.
  for (const name of ['PULSE_SINK', 'MIC_SILENCE_SINK', 'BOT_MIC_SOURCE']) {
    const n = code.split('\n').filter((l) => new RegExp(`^\\s*(?:export\\s+)?${name}=`).test(l)).length;
    assertEqual(
      n,
      1,
      `[M126] ${name} is assigned EXACTLY ONCE — a later reassignment would make every resolved-value assertion here describe a value bash never used at the pactl line`,
    );
  }
  // CONTROL: the counter must be able to see a second assignment. Without this
  // a regex that matched nothing would report 0 and... fail, but a regex that
  // matched everything once would pass for the wrong reason.
  {
    const twoAssignments = code + '\nMIC_SILENCE_SINK="mic_silence_sink"\n';
    const n = twoAssignments.split('\n').filter((l) => /^\s*(?:export\s+)?MIC_SILENCE_SINK=/.test(l)).length;
    assertEqual(n, 2, '[M126] CONTROL: the counter really does see a second assignment');
  }

  // FIXTURE: the resolver really did bind all three, or every assertion below
  // would compare empty strings and pass for the wrong reason.
  assertEqual(resolvedPulse, 'zoom_sink', '[M125] FIXTURE: PULSE_SINK resolves');
  assertEqual(resolvedMicSink, 'mic_silence_sink', '[M125] FIXTURE: MIC_SILENCE_SINK resolves');
  assertEqual(resolvedMicSrc, 'bot_mic', '[M125] FIXTURE: BOT_MIC_SOURCE resolves');
  assertEqual(new Set([resolvedPulse, resolvedMicSink, resolvedMicSrc]).size, 3, '[M125] and all three are distinct after resolution');

  const argsOf = (verb: string): string[] =>
    code
      .split('\n')
      .filter((l) => l.includes(`pactl ${verb}`))
      .map((l) => {
        const m = new RegExp(`pactl ${verb}\\s+"?([^"\\s]+)"?`).exec(l);
        return m ? resolveShell(m[1], vars) : '';
      })
      .filter((v) => v.length > 0);

  // DEFAULT SINK: every assignment must resolve to the RECORDING sink. If any
  // resolved to the mic's silent sink, Zoom would render the meeting into it and
  // the recording would be empty.
  const defaultSinks = argsOf('set-default-sink');
  assertEqual(defaultSinks.length >= 1, true, '[M125] a default sink is set');
  assertEqual(
    defaultSinks.every((v) => v === resolvedPulse),
    true,
    '[M125] EVERY resolved set-default-sink argument is the recording sink',
  );
  assertEqual(defaultSinks.includes(resolvedMicSink), false, '[M125] and none of them resolves to the silent mic sink');

  // DEFAULT SOURCE: must resolve to the bot mic, and must never be a monitor
  // (a monitor as the default capture device is the echo loop by another route).
  const defaultSources = argsOf('set-default-source');
  assertEqual(defaultSources, [resolvedMicSrc], '[M125] the default source resolves to the bot mic');
  assertEqual(defaultSources.some((v) => v.includes('.monitor')), false, '[M125] and is never a .monitor device');

  // SOURCE MUTE: must be applied to the bot mic itself.
  assertEqual(argsOf('set-source-mute'), [resolvedMicSrc], '[M125] the source mute is applied to the resolved bot mic');

  // The two null sinks must be genuinely different devices after resolution.
  const nullSinkNames = code
    .split('\n')
    .filter((l) => l.includes('module-null-sink'))
    .map((l) => {
      const m = /sink_name="?([^"\s]+)"?/.exec(l);
      return m ? resolveShell(m[1], vars) : '';
    })
    .filter((v) => v.length > 0);
  assertEqual(nullSinkNames.length, 2, '[M125] exactly two null sinks are created');
  assertEqual(new Set(nullSinkNames).size, 2, '[M125] and they are DIFFERENT devices after resolution (not one aliased to the other)');
  assertEqual(nullSinkNames.includes(resolvedPulse), true, '[M125] one is the recording sink');
  assertEqual(nullSinkNames.includes(resolvedMicSink), true, '[M125] the other is the silent mic feed');
}

// 97. [M89] The mic source must NOT be named virtual_mic: services/tts-playback.ts
//     runs `pactl set-source-mute virtual_mic 0` — it would UNMUTE our mic. A
//     distinct name makes that unreachable by construction rather than by
//     remembering not to call it.
{
  const sh = readZoomBotStartScript();
  // Comment lines are stripped first. Both names appear in the block's
  // explanation ON PURPOSE — that prose is why the distinct naming exists — and
  // a negative assertion tripped by a comment is the same comment-vs-code
  // confusion that has now caught three assertions in this file. See codeOnly().
  const code = sh
    .split('\n')
    .filter((l) => !l.trimStart().startsWith('#'))
    .join('\n');
  assertEqual(code.includes('virtual_mic'), false, '[M89] no EXECUTED line names virtual_mic (tts-playback.ts unmutes that literal name)');
  assertEqual(code.includes('tts_sink'), false, '[M89] and none names tts_sink — it does not exist in this container');
  assertEqual(code.includes('BOT_MIC_SOURCE="bot_mic"'), true, '[M89] the mic source has its own distinct name');
  // Guard the guard: if the comment prose were deleted the assertions above
  // would still pass, so this pins that the REASON is recorded next to the code.
  assertEqual(sh.includes('tts-playback.ts unmutes a source by that literal name'), true, '[M89] and the script records WHY the name must differ');
}

// 98. [M90] THE RECORDING PATH IS UNTOUCHED. parecord takes an EXPLICIT
//     --device, so it never consults the default source, and $PULSE_SINK, its
//     monitor and its default-sink status are all unchanged by this edit. This
//     asserts both halves — the producer name in start.sh and the consumer
//     device string in recording.ts — because a change to either alone would
//     silently break the transcript.
{
  const sh = readZoomBotStartScript();
  assertEqual(sh.includes('export PULSE_SINK="${PULSE_SINK:-zoom_sink}"'), true, '[M90] $PULSE_SINK is still zoom_sink by default');
  assertEqual(
    sh.includes('pactl load-module module-null-sink sink_name="$PULSE_SINK"'),
    true,
    '[M90] the recording sink is still loaded under $PULSE_SINK',
  );
  // recording.ts lives under zoom/web, so the existing locator handles it.
  const rec = readZoomWebSource('recording.ts');
  assertEqual(
    rec.includes("`--device=${process.env.PULSE_SINK || 'zoom_sink'}.monitor`"),
    true,
    '[M90] parecord still captures ${PULSE_SINK}.monitor via an EXPLICIT --device (never the default source)',
  );
}

console.log('\n=== W11: the post-click re-read is PINNED to the clicked element ===');

// Two distinct elements, distinct stable keys. A is the one that gets clicked;
// B is an unrelated footer control that sorts EARLIER in the probe order — which
// is what made the old `selectZoomMicToggle(afterCandidates)` attribute B's
// reading to a click on A.
const keyA = 'div:2/div:0/button:0';
const keyB = 'div:2/div:0/button:3';

const candA = (p: Partial<ZoomMicProbe>): ZoomMicCandidate =>
  ({ selector: 'button.join-audio-container__btn', index: 0, probe: probe({ elementKey: keyA, ...p }) });
const candB = (p: Partial<ZoomMicProbe>): ZoomMicCandidate =>
  ({ selector: 'footer button[aria-label*="mute" i]', index: 3, probe: probe({ elementKey: keyB, ...p }) });

const clickedA: ZoomMicSelection = {
  candidate: candA({ ariaLabel: 'audio', descendantClassNames: ['SvgAudioUnmuted'] }),
  reading: { kind: 'unmuted', evidence: 'class hint "svgaudiounmuted"' },
};

const clickReport = (after: ZoomMicSelection | null, afterDetail = 'd') =>
  reportZoomMuteClick({
    reason: 'confirmed unmuted 2x — re-muting (click #1)',
    before: clickedA,
    after,
    afterDetail,
    priorIneffectiveClicks: 0,
  });

// 99. [M91] FALSE `muted` MUST BE IMPOSSIBLE. The click lands on A and does
//     nothing; on re-probe A no longer reads confidently, and B — earlier in the
//     probe order — reads muted. The old code logged "CONFIRMED the bot muted
//     after clicking" from B's reading and never retired A.
{
  const afterCandidates = [
    candB({ ariaLabel: 'Unmute' }),                       // reads MUTED, sorts first
    candA({ ariaLabel: 'audio' }),                        // the clicked element: no confident reading
  ];

  // FIXTURE HAZARD CHECK — without this the test could pass simply because
  // nothing reads muted, which would make it vacuous. This asserts the trap is
  // actually present: the OLD selection strategy really does return `muted`.
  const oldStrategy = selectZoomMicToggle(afterCandidates);
  assertEqual(oldStrategy !== null && oldStrategy.reading.kind, 'muted', '[M91] FIXTURE: the old fresh-selection strategy WOULD have read muted');
  assertEqual(oldStrategy !== null && oldStrategy.candidate.probe.elementKey, keyB, '[M91] FIXTURE: ...from element B, not the clicked element A');

  const after = readZoomMicCandidateByKey(afterCandidates, keyA);
  assertEqual(after !== null && after.candidate.probe.elementKey, keyA, '[M91] the re-read is taken from the CLICKED element');
  const r = clickReport(after);
  assertEqual(r.verdict, 'unreadable', '[M91] verdict is unreadable — NOT muted from a different element');
  assertEqual(r.warn, true, '[M91] and it is a warning, not a silent success');
  assertEqual(r.message.includes('CONFIRMED'), false, '[M91] the word CONFIRMED never appears');
  assertEqual(r.message.includes('re-read as "not-mute-toggle"'), true, '[M91] the line says what the CLICKED element actually read');
  assertEqual(r.rejectKey, null, '[M91] and an unknown never retires a candidate');
}

// 100. [M92] FALSE `still-unmuted` MUST NOT RETIRE A WORKING CONTROL. The click
//      WORKS on A (A now reads muted), but B reads unmuted and sorts first. The
//      old code would have retired A — permanently, for the session — which is
//      the worse direction: A is the control that works.
{
  const afterCandidates = [
    candB({ ariaLabel: 'Mute' }),                                  // reads UNMUTED, sorts first
    candA({ descendantClassNames: ['SvgAudioMuted'] }),            // the clicked element: muted, i.e. it worked
  ];

  const oldStrategy = selectZoomMicToggle(afterCandidates);
  assertEqual(oldStrategy !== null && oldStrategy.reading.kind, 'unmuted', '[M92] FIXTURE: the old strategy WOULD have read unmuted');
  assertEqual(oldStrategy !== null && oldStrategy.candidate.probe.elementKey, keyB, '[M92] FIXTURE: ...from element B, so A would have been retired');

  const r = clickReport(readZoomMicCandidateByKey(afterCandidates, keyA));
  assertEqual(r.verdict, 'muted', '[M92] verdict is muted — the clicked element did mute');
  assertEqual(r.rejectKey, null, '[M92] the WORKING control is NOT retired');
  assertEqual(r.warn, false, '[M92] and a successful mute is not a warning');
  assertEqual(r.message.includes(`re-read of the SAME element (key=${keyA})`), true, '[M92] the line names the element the verdict describes');
}

// 101. [M93] KEY STABILITY ACROSS A MUTE-STATE CHANGE — the common success path.
//      A successful mute flips the icon class, so if elementKey included `class`
//      (or aria-label) the clicked element would fail to match on EVERY success
//      and every working click would report "unreadable". The key excludes both
//      for exactly this reason; this pins that it still holds.
{
  // W11: the before-state uses a RENDERED glyph, so it reads actionable
  // `unmuted` on the precise tier. A hidden class hint would now read
  // `unmuted-unconfirmed` and could never have triggered the click this test is
  // about — the fixture has to be a state the watcher would really act on.
  const before = candA({ ariaLabel: 'audio', visibleDescendantClassNames: ['SvgAudioUnmuted'] });
  const afterSameElement = candA({ ariaLabel: 'Unmute', descendantClassNames: ['SvgAudioMuted'] });
  assertEqual(before.probe.elementKey, afterSameElement.probe.elementKey, '[M93] the key survives a class AND aria-label change');
  assertEqual(readZoomMicState(before.probe).kind, 'unmuted', '[M93] FIXTURE: the before-state really does read unmuted');
  assertEqual(readZoomMicState(afterSameElement.probe).kind, 'muted', '[M93] FIXTURE: and the after-state really does read muted');
  const r = clickReport(readZoomMicCandidateByKey([afterSameElement], before.probe.elementKey));
  assertEqual(r.verdict, 'muted', '[M93] so a working click is reported as muted, not as unreadable');
}

// 102. [M94] The clicked element ABSENT from the re-probe is an honest unknown —
//      never a verdict borrowed from whatever else is on screen.
{
  const afterCandidates = [candB({ ariaLabel: 'Unmute' })]; // only B survives the re-probe
  assertEqual(readZoomMicCandidateByKey(afterCandidates, keyA), null, '[M94] a missing clicked element yields null, not a substitute');
  const r = clickReport(null, 'button.x[0] key=div:2/div:0/button:3 ...');
  assertEqual(r.verdict, 'unreadable', '[M94] verdict unreadable');
  assertEqual(r.message.includes(`the clicked element (key=${keyA}) was NOT FOUND in the post-click probe`), true, '[M94] and the line says the element was not found');
  assertEqual(r.rejectKey, null, '[M94] nothing is retired');
}

// 103. [M95] readZoomMicCandidateByKey itself, including the fail-safe paths.
{
  const b = candB({ ariaLabel: 'Unmute' });
  const a = candA({ ariaLabel: 'Mute' });
  const set = [b, a];

  const gotA = readZoomMicCandidateByKey(set, keyA);
  assertEqual(gotA !== null && gotA.candidate.probe.elementKey, keyA, '[M95] returns the requested element even when a LATER match');
  assertEqual(gotA !== null && gotA.reading.kind, 'unmuted', '[M95] read on its own evidence');

  const gotB = readZoomMicCandidateByKey(set, keyB);
  assertEqual(gotB !== null && gotB.reading.kind, 'muted', '[M95] and the other element reads independently');

  // An EMPTY key must never match. '' is the default elementKey, so a key-less
  // probe would otherwise match a key-less request and hand back an arbitrary
  // element — the "control accidentally equal to the ambient value" shape.
  const keyless = [{ selector: 's', index: 0, probe: probe({ ariaLabel: 'Unmute' }) }];
  assertEqual(keyless[0].probe.elementKey, '', '[M95] FIXTURE: a key-less probe really does have the ambient empty key');
  assertEqual(readZoomMicCandidateByKey(keyless, ''), null, '[M95] an empty key matches NOTHING, even a key-less candidate');
  assertEqual(readZoomMicCandidateByKey(set, 'div:9/div:9/button:9'), null, '[M95] an unknown key matches nothing');
  assertEqual(readZoomMicCandidateByKey([], keyA), null, '[M95] an empty probe set yields null');

  // A non-confident reading is RETURNED, not filtered out — that is what lets
  // the caller say "the clicked element read not-mute-toggle" instead of
  // silently falling through to another element.
  const unreadableA = readZoomMicCandidateByKey([candA({ ariaLabel: 'audio' })], keyA);
  assertEqual(unreadableA !== null, true, '[M95] a non-confident reading is still returned');
  assertEqual(unreadableA !== null && unreadableA.reading.kind, 'not-mute-toggle', '[M95] with its real kind, for the caller to report honestly');
}

// 104. [M96] An empty key on the CLICKED element degrades to unreadable rather
//      than to a borrowed verdict. Nothing can be verified without an identity.
{
  const clickedKeyless: ZoomMicSelection = {
    candidate: { selector: 's', index: 0, probe: probe({ ariaLabel: 'Mute' }) },
    reading: { kind: 'unmuted', evidence: 'aria-label "mute" offers mute' },
  };
  const afterCandidates = [candB({ ariaLabel: 'Unmute' })];
  const r = reportZoomMuteClick({
    reason: 'r',
    before: clickedKeyless,
    after: readZoomMicCandidateByKey(afterCandidates, clickedKeyless.candidate.probe.elementKey),
    afterDetail: 'd',
    priorIneffectiveClicks: 0,
  });
  assertEqual(r.verdict, 'unreadable', '[M96] no identity => unverifiable, not a borrowed reading');
  assertEqual(r.rejectKey, null, '[M96] and nothing is retired');
  assertEqual(r.message.includes('key=none'), true, '[M96] the log states there was no element identity');
}

console.log('\n=== W11: source guards — both re-read sites are pinned by key ===');

// 105. The two Playwright call sites. REPLACES the guard that previously pinned
//      `after: verifyError ? null : selectZoomMicToggle(afterCandidates),` —
//      that assertion codified the defect, so asserting it was true is what let
//      the bug look deliberate. The negative assertion is now the load-bearing
//      one: the fresh-selection strategy must not reappear on either re-read.
{
  const src = readPrepareSource();

  // (a) the mute watcher
  const wStart = src.indexOf('export function startZoomMuteWatcher');
  const wFn = src.slice(wStart, src.indexOf('\n}\n', wStart));
  // REWRITTEN for the settle poll: the keying moved into pollZoomMuteSettled's
  // argument list, which reads the clicked element on every poll.
  assertEqual(
    wFn.includes('selection.candidate.probe.elementKey,\n              pollConfig,'),
    true,
    '[M97] the watcher re-read is keyed to the CLICKED element on every poll',
  );
  assertEqual(wFn.includes('after: verifyError || !poll ? null : poll.after,'), true, '[M97] and the verdict is built from that keyed reading');
  assertEqual(
    wFn.includes('selectZoomMicToggle(afterCandidates'),
    false,
    '[M97] and the fresh-selection strategy is GONE from the watcher re-read',
  );
  // The selection that CHOOSES what to click must still use selectZoomMicToggle
  // — only the re-read is keyed. Guards against over-applying the fix.
  assertEqual(wFn.includes('selectZoomMicToggle(candidates, rejected)'), true, '[M97] choosing WHAT to click still uses the ranked selection');

  // (b) ensureZoomMutedInMeeting — the same defect in the join-time path, which
  //     could report "Muted microphone in meeting" and return muted:true from a
  //     different element than it clicked.
  const eStart = src.indexOf('export async function ensureZoomMutedInMeeting');
  const eFn = src.slice(eStart, src.indexOf('\n}\n', eStart));
  assertEqual(eStart > -1, true, '[M98] ensureZoomMutedInMeeting is present');
  assertEqual(
    eFn.includes('readZoomMicCandidateByKey(afterProbe, selection.candidate.probe.elementKey)'),
    true,
    '[M98] its post-click re-read is keyed to the clicked element too',
  );
  assertEqual(
    eFn.includes('selectZoomMicToggle(await probeZoomMicCandidates'),
    false,
    '[M98] and its fresh-selection re-read is gone',
  );
  assertEqual(eFn.includes('selectZoomMicToggle(candidates)'), true, '[M98] while its initial choice of control is unchanged');
}

console.log('\n=== W12: mute vocabulary — STATE words must not read as an action offered ===');

// 106. [M99] THE DANGEROUS DEFECT. The text tier matched `mute` as a bare
//      substring and called it "the action offered", so "Muted" / "You are
//      muted" read as UNMUTED -> the watcher clicks -> a muted bot becomes
//      UNMUTED. That is the one direction that makes the reported bug worse,
//      and it came from the text tier itself.
{
  const rows: Array<[string, 'muted' | 'unmuted', 'state' | 'action']> = [
    ['muted', 'muted', 'state'],
    ['you are muted', 'muted', 'state'],
    ['your microphone is muted', 'muted', 'state'],
    ['unmuted', 'unmuted', 'state'],
    ['you are unmuted', 'unmuted', 'state'],
    ['mute', 'unmuted', 'action'],
    ['unmute', 'muted', 'action'],
    ['unmute my microphone', 'muted', 'action'],
  ];
  for (const [text, kind, sense] of rows) {
    const v = readZoomMuteVocabulary(text);
    assertEqual(v !== null && v.kind, kind, `[M99] "${text}" => ${kind}`);
    assertEqual(v !== null && v.sense, sense, `[M99] "${text}" is a ${sense} word`);
  }
  assertEqual(readZoomMuteVocabulary(''), null, '[M99] empty string carries no vocabulary');
  assertEqual(readZoomMuteVocabulary('audio'), null, '[M99] "audio" carries no vocabulary');
  assertEqual(readZoomMuteVocabulary('reactions'), null, '[M99] an unrelated word carries none');
}

// 107. [M100] The same polarity through the READER, on both the text tier and
//      the aria-label tier — the state-word hazard existed on both.
{
  for (const field of ['labelText', 'text'] as const) {
    const p = field === 'labelText' ? probe({ labelText: 'Muted' }) : probe({ text: 'Muted' });
    const r = readZoomMicState(p);
    assertEqual(r.kind, 'muted', `[M100] ${field} "Muted" => muted, NOT unmuted (it is a state, not an offer)`);
    assertEqual(r.evidence.includes('states "muted"'), true, `[M100] and the evidence says it read a STATE`);
  }
  const aria = readZoomMicState(probe({ ariaLabel: 'Muted' }));
  assertEqual(aria.kind, 'muted', '[M100] aria-label "Muted" => muted');
  assertEqual(aria.evidence.includes('states "muted"'), true, '[M100] with the same state/action distinction');

  // The action words keep their original, live-confirmed meaning.
  assertEqual(readZoomMicState(probe({ ariaLabel: 'Mute' })).kind, 'unmuted', '[M100] aria-label "Mute" still => unmuted (action offered)');
  assertEqual(readZoomMicState(probe({ ariaLabel: 'Mute' })).evidence.includes('offers mute'), true, '[M100] and still reads as an OFFER');

  // The whole point: a state word must AGREE with the icon whitelist instead of
  // contradicting it. Fixture makes both present and consistent.
  const agree = readZoomMicState(probe({ text: 'You are muted', visibleDescendantClassNames: ['SvgAudioMuted'] }));
  assertEqual(agree.kind, 'muted', '[M100] a muted state word and a muted icon now AGREE');
}

// 108. [M101] The non-mic guard still wins over the vocabulary reader, on text
//      as well as aria-label. "Mute All" must never read as a mic state.
{
  assertEqual(readZoomMicState(probe({ text: 'Mute All' })).kind, 'not-mic-control', '[M101] "Mute All" as text is still rejected first');
  assertEqual(readZoomMicState(probe({ labelText: 'Ask to Unmute' })).kind, 'not-mic-control', '[M101] and "Ask to Unmute"');
}

console.log('\n=== W15: contradicting state glyphs resolve to MUTED, and the tier order cannot decide ===');

// 108b. [M122] QA hole 2. Swapping the two whitelist lookups was a
//       behaviour-preserving edit for every single-glyph fixture, so the suite
//       stayed green — while with BOTH glyphs rendered the order alone decided
//       whether a MUTED bot read `unmuted` and got clicked. Fixed by computing
//       both lookups before either return and resolving the conflict toward
//       `muted`, which removes the ordering dependency entirely rather than
//       pinning one arbitrary order.
{
  const both = readZoomMicState(probe({ visibleDescendantClassNames: ['SvgAudioMuted', 'SvgAudioUnmuted'] }));
  assertEqual(both.kind, 'muted', '[M122] two contradicting VISIBLE glyphs resolve to muted (the safe direction)');
  assertEqual(both.evidence.includes('CONFLICTING visible icon classes'), true, '[M122] and the evidence names the conflict rather than hiding it');
  assertEqual(both.kind === 'unmuted', false, '[M122] it is never the direction that triggers a click');

  // Reversed fixture order: the RESULT must be identical. If the reading
  // depended on which glyph the prober happened to list first, this goes red.
  const reversed = readZoomMicState(probe({ visibleDescendantClassNames: ['SvgAudioUnmuted', 'SvgAudioMuted'] }));
  assertEqual(reversed.kind, 'muted', '[M122] and the DOM order of the two glyphs does not change the verdict');

  // CONTROLS — the conflict rule must not swallow the single-glyph paths, which
  // are the normal case and are what M3/M69 pin.
  assertEqual(readZoomMicState(probe({ visibleDescendantClassNames: ['SvgAudioUnmuted'] })).kind, 'unmuted', '[M122] CONTROL: an unmuted glyph ALONE still reads unmuted');
  assertEqual(readZoomMicState(probe({ visibleDescendantClassNames: ['SvgAudioMuted'] })).kind, 'muted', '[M122] CONTROL: a muted glyph alone still reads muted');
  assertEqual(
    readZoomMicState(probe({ visibleDescendantClassNames: ['SvgAudioUnmuted'] })).evidence.includes('CONFLICTING'),
    false,
    '[M122] CONTROL: and a single glyph is never reported as a conflict',
  );
}

// 108c. [M123] The same dangerous direction in the WEAK substring tier. This one
//       CANNOT be reordered the way the whitelist can: 'svgaudiounmuted' contains
//       'muted', so a blob-level "both matched" test is true for every
//       unmuted-ONLY control and would make the bot never mute itself — the M3
//       trap. Conflict detection is therefore per TOKEN.
{
  const both = readZoomMicState(probe({ descendantClassNames: ['SvgAudioMuted', 'SvgAudioUnmuted'] }));
  assertEqual(both.kind, 'muted', '[M123] two contradicting class-hint TOKENS resolve to muted');
  assertEqual(both.evidence.includes('CONFLICTING class hints'), true, '[M123] naming the conflict, and marking it as the weak tier');

  // THE M3 TRAP, asserted directly: an unmuted-only control must NOT be read as
  // a conflict just because its token contains the substring "muted".
  // The CONTROLs assert the non-conflict direction. They now expect
  // `unmuted-unconfirmed` (W11 demoted the actionable reading), which is still
  // a distinct kind from the `muted` a blob-level conflict test would produce —
  // so the M3 trap remains caught.
  const unmutedOnly = readZoomMicState(probe({ descendantClassNames: ['SvgAudioUnmuted'] }));
  assertEqual(unmutedOnly.kind, 'unmuted-unconfirmed', '[M123] CONTROL: "svgaudiounmuted" alone is NOT a conflict (it merely contains "muted")');
  assertEqual(unmutedOnly.evidence.includes('CONFLICTING'), false, '[M123] CONTROL: and is not reported as one');
  const bareUnmuted = readZoomMicState(probe({ descendantClassNames: ['something--unmuted-state'] }));
  assertEqual(bareUnmuted.kind, 'unmuted-unconfirmed', '[M123] CONTROL: the bare-word fallback still survives a rename');
  assertEqual(bareUnmuted.evidence.includes('CONFLICTING'), false, '[M123] CONTROL: and is not a conflict either');
}

// 108d. [M124] REMOVED, deliberately. It asserted `both lookups precede the
//       first return` and the exact text `if (unmutedIcon && mutedIcon) {` —
//       a SPELLING LOCK on a property test 108b already proves behaviourally,
//       and one that would redden on a cosmetic `const` rename or reorder with
//       no behaviour change. Same class as the old M58 guard that codified a
//       defect. The behavioural cover is complete: reintroducing an early
//       `return` before the second lookup makes both-glyphs read `unmuted`
//       again, which reddens '[M122] two contradicting VISIBLE glyphs resolve to
//       muted' and '[M122] it is never the direction that triggers a click'.
//       Verified by mutation, not assumed.

console.log('\n=== W12: aria-pressed ranks BELOW visible text, deliberately ===');

// 109. [M102] W4: the ordering is now a decision, not an accident. A signal
//      never OBSERVED on Zoom's control, whose polarity for a mic toggle is
//      genuinely ambiguous, does not outrank a word rendered on screen.
//      Fixture supplies BOTH and makes them CONTRADICT, so the winner is
//      unambiguous — no test did that before.
{
  const r = readZoomMicState(probe({ labelText: 'Mute', ariaPressed: 'true' }));
  assertEqual(r.kind, 'unmuted', '[M102] visible text WINS over aria-pressed when they disagree');
  assertEqual(r.evidence.includes('label node'), true, '[M102] and the evidence names the text');
  assertEqual(r.evidence.includes('aria-pressed'), false, '[M102] not aria-pressed');

  // aria-pressed still beats the class tiers, which is what it was always for.
  const overClass = readZoomMicState(probe({ ariaPressed: 'true', visibleDescendantClassNames: ['SvgAudioUnmuted'] }));
  assertEqual(overClass.kind, 'muted', '[M102] aria-pressed still outranks a contradicting icon whitelist');
  // And "false" is still never read as unmuted — the asymmetric-cost rule.
  assertEqual(readZoomMicState(probe({ ariaPressed: 'false' })).kind, 'unknown', '[M102] aria-pressed="false" is still NOT unmuted');
}

console.log('\n=== W13: retirement is CORROBORATED and REVERSIBLE (Critical 2) ===');

// 115. [M108] One proven failure is a STRIKE, not a retirement. This is the
//      hardening that makes a surviving timing artefact non-terminal.
{
  const k = 'path:div:2/div:0/button:0';
  const s1 = stepZoomRetirement(zoomRetirementInitialState, k);
  assertEqual(s1.action, 'strike', '[M108] the FIRST proven failure is only a strike');
  assertEqual(s1.state.retired.length, 0, '[M108] nothing is retired yet');
  assertEqual(s1.state.strikes[k], 1, '[M108] and the strike is recorded against the key');
  assertEqual(s1.reason.includes('1/2'), true, '[M108] the reason states the count');

  const s2 = stepZoomRetirement(s1.state, k);
  assertEqual(s2.action, 'retire', '[M108] the SECOND strike on the same key retires it');
  assertEqual(s2.state.retired, [k], '[M108] and it appears in the retired list');

  const s3 = stepZoomRetirement(s2.state, k);
  assertEqual(s3.action, 'none', '[M108] a third report on an already-retired key is a no-op');
  assertEqual(s3.state, s2.state, '[M108] and does not mutate the state');
}

// 116. [M109] Strikes are PER KEY: one failure on each of two elements must not
//      retire either. Accumulation across passes, which nothing tested before.
{
  const a = 'path:div:2/div:0/button:0';
  const b = 'path:div:2/div:0/button:3';
  let st = zoomRetirementInitialState;
  st = stepZoomRetirement(st, a).state;
  st = stepZoomRetirement(st, b).state;
  assertEqual(st.retired.length, 0, '[M109] one strike each retires neither');
  assertEqual(st.strikes[a], 1, '[M109] a has one strike');
  assertEqual(st.strikes[b], 1, '[M109] b has one strike');
  const second = stepZoomRetirement(st, a);
  assertEqual(second.action, 'retire', '[M109] a second strike on A retires A');
  assertEqual(second.state.retired, [a], '[M109] and only A');
  assertEqual(second.state.strikes[b], 1, "[M109] B's strike is untouched");
}

// 117. [M110] The fail-safes, unchanged: an empty key is never recorded and
//      never retired ('' is the ambient elementKey and would match every
//      unidentified element).
{
  const r = stepZoomRetirement(zoomRetirementInitialState, '');
  assertEqual(r.action, 'none', '[M110] an empty key is never recorded');
  assertEqual(r.state.retired.length, 0, '[M110] and never retired');
  assertEqual(Object.keys(r.state.strikes).length, 0, '[M110] not even as a strike');
  const twice = stepZoomRetirement(stepZoomRetirement(zoomRetirementInitialState, '').state, '');
  assertEqual(twice.state.retired.length, 0, '[M110] and repeating it never accumulates to a retirement');
}

// 118. [M111] "EVERY readable control retired" must NOT be absorbing. Without
//      this the watcher logs `N candidate(s) rejected` for the rest of the call
//      with nothing left to try — strictly worse than the loop it replaced.
{
  const a = 'path:a';
  const b = 'path:b';
  const cands: ZoomMicCandidate[] = [
    { selector: 's1', index: 0, probe: probe({ elementKey: a, ariaLabel: 'Mute' }) },
    { selector: 's2', index: 0, probe: probe({ elementKey: b, ariaLabel: 'Mute' }) },
  ];
  assertEqual(zoomRetirementIsAbsorbing(cands, new Set()), false, '[M111] nothing retired => not absorbing');
  assertEqual(zoomRetirementIsAbsorbing(cands, new Set([a])), false, '[M111] one of two retired => still options left');
  assertEqual(zoomRetirementIsAbsorbing(cands, new Set([a, b])), true, '[M111] ALL readable controls retired => ABSORBING');

  // A retired key that is not on screen is not absorbing — there is simply
  // nothing readable, which is a different situation and already logged as
  // "no selectable mic control".
  assertEqual(zoomRetirementIsAbsorbing([], new Set([a])), false, '[M111] no candidates at all => not absorbing (a different failure)');
  const unreadable: ZoomMicCandidate[] = [{ selector: 's', index: 0, probe: probe({ elementKey: a, ariaLabel: 'audio' }) }];
  assertEqual(zoomRetirementIsAbsorbing(unreadable, new Set([a])), false, '[M111] only UNREADABLE candidates => not absorbing');

  const cleared = resetZoomRetirement({ strikes: { [a]: 2, [b]: 2 }, retired: [a, b], resets: 0 });
  assertEqual(cleared.retired, [], '[M111] the reset clears every retirement');
  assertEqual(Object.keys(cleared.strikes).length, 0, '[M111] and every strike, so discovery starts clean');
  assertEqual(cleared.resets, 1, '[M111] counting the reset, so a loop is visible in the log');
  assertEqual(zoomRetirementIsAbsorbing(cands, new Set(cleared.retired)), false, '[M111] and the state is no longer absorbing');
}

console.log('\n=== W14: the SEND-SIDE canary (W9/Q5) ===');

// 119. [M112] `AUDIO JOIN OK` counts INBOUND <audio> elements and printed
//      identically on the failed run, so it is not a canary for the microphone
//      fix. This line is. audioInputDevices=0 IS the 2026-09-02 root cause.
{
  const none = describeZoomSendSide({ audioInputDevices: 0, error: null }, 'off');
  assertEqual(none.includes('NO CAPTURE DEVICE'), true, '[M112] zero devices is named as the root cause, unambiguously');
  assertEqual(none.includes('audioInputDevices=0'), true, '[M112] with the count');
  assertEqual(none.includes('start.sh'), true, '[M112] and points at where to look');

  const ok = describeZoomSendSide({ audioInputDevices: 1, error: null }, 'off');
  assertEqual(ok.includes('CAPTURE DEVICE IS OFFERED'), true, '[M112] a device present is stated plainly');
  assertEqual(ok.includes('NO CAPTURE DEVICE'), false, '[M112] and not confusable with the failure line');

  assertEqual(describeZoomSendSide(null, 'on').includes('unreadable'), true, '[M112] an unrunnable probe reads unreadable, never 0');
  assertEqual(
    describeZoomSendSide({ audioInputDevices: 0, error: 'boom' }, 'on').includes('unreadable (boom)'),
    true,
    '[M112] and an error is reported rather than fabricating a zero',
  );
}

// 120. [M113] Q5: `tracksLocked` is STRUCTURALLY 0 in mode off — which is the
//      mode the first live run uses. An operator must be told, not left reading
//      a counter that cannot be anything else.
{
  const off = describeZoomSendSide({ audioInputDevices: 1, error: null }, 'off');
  assertEqual(off.includes('STRUCTURALLY 0'), true, '[M113] mode off says tracksLocked cannot be non-zero');

  // [M129] THE FALSE HALF, now pinned. The old note claimed "the peer registry
  // is not installed, so audioSenders reads unreadable" — false in BOTH claims,
  // and contradicted by this file's own KILL SWITCH BOUNDARY comment: the
  // RTCPeerConnection ctor patch is deliberately NOT gated, so in mode off the
  // registry IS populated and audioSenders is live. Mode off IS the first live
  // run, and audioSenders/sweepDisabled is the only page-side evidence that Zoom
  // ATTACHED the mic to a sender rather than merely being offered a device — so
  // the note instructed the operator to discard the informative signal for the
  // weaker one. A negative guard, because a wrong operator note rots back in.
  assertEqual(
    off.includes('registry is not installed'),
    false,
    '[M129] the off-note must NEVER claim the peer registry is not installed — the ctor patch is above the kill-switch boundary',
  );
  assertEqual(off.includes('unreadable'), false, '[M129] nor that audioSenders is unreadable in mode off');
  assertEqual(off.includes('the peer registry IS installed'), true, '[M129] it states the registry IS installed');
  assertEqual(off.includes('audioSenders and sweepDisabled are LIVE'), true, '[M129] and that those counters are live');
  assertEqual(
    off.includes('ATTACHED the mic to a sender'),
    true,
    '[M129] and says WHY they matter — attachment, not mere availability, is what audioInputDevices cannot show',
  );

  const noneMode = describeZoomSendSide({ audioInputDevices: 1, error: null }, 'none');
  assertEqual(noneMode.includes('there is NO heartbeat at all'), true, '[M113] mode none says the guard is not armed');

  const on = describeZoomSendSide({ audioInputDevices: 1, error: null }, 'on');
  assertEqual(on.includes('are all meaningful'), true, '[M113] mode on says the counters are usable');
  assertEqual(on.includes('STRUCTURALLY 0'), false, '[M113] and does not carry the off-mode caveat');
  assertEqual(on.includes('registry is not installed'), false, '[M129] and no mode note carries the false registry claim');
}

// 121. [M114] The seal-off lock description had the same defect in miniature: it
//      printed `tracksDisabled=0` for tracks that WERE disabled.
{
  const d = describeOutboundAudioLock({
    sealEnabled: false, skippedVoiceAgent: false, alreadyInstalled: true, registryPresent: true,
    tracksLocked: 0, blockedUnmutes: 0, tracksVerified: 0, tracksResealed: 0,
    patchedConstructor: false, patchedAddTrack: false, patchedAddTransceiver: false,
    patchedReplaceTrack: false, patchedGetUserMedia: false, errors: 0,
  });
  assertEqual(d.includes('tracksDisabled='), false, '[M114] the mislabel "tracksDisabled" is gone');
  assertEqual(d.includes('STRUCTURALLY 0 in this mode'), true, '[M114] and the zero is explained');
  assertEqual(d.includes('tracks ARE still written enabled=false'), true, '[M114] stating what actually happened to the track');
}

console.log('\n=== W14: remaining warning fixes (source guards) ===');

// 121b. [M120/M121] The SEND-SIDE canary in index.ts. This was a real gap found
//       by mutation: deleting the probe, and computing the line but never
//       logging it, BOTH survived the suite. It is the only line that reports
//       whether the start.sh microphone fix is in force, so it needs guarding.
{
  const indexSrc = readZoomWebSource('index.ts');
  assertEqual(indexSrc.includes('const sendSide = await probeZoomSendSide(page);'), true, '[M120] index.ts runs the send-side probe');
  // Statement position, exactly as written — the M41 lesson: `void x` satisfies
  // a bare includes() check on the identifier, and that is precisely the
  // mutation that survived here.
  assertEqual(
    indexSrc.includes('log(`[Zoom Web] SEND-SIDE CHECK — ${describeZoomSendSide(sendSide, zoomAudioLockMode())}'),
    true,
    '[M121] and LOGS it — a computed-but-unlogged canary is no canary',
  );
  assertEqual(codeOnly(indexSrc).includes('void describeZoomSendSide'), false, '[M121] it is never dead-coded');
  // It must run in EVERY mode: it patches nothing and touches no track, and in
  // hands-off mode it is the only send-side evidence available. So it must sit
  // OUTSIDE the track-touching gate — brace-matched, not merely ordered.
  const gateIdx = indexSrc.indexOf('if (!voiceAgentEnabled && touchAllowed) {');
  const gateBlock = extractBraceBlock(indexSrc, gateIdx);
  assertEqual(gateBlock !== null, true, '[M120] the gate block is extractable');
  assertEqual(
    gateBlock !== null && gateBlock.includes('probeZoomSendSide'),
    false,
    '[M120] the send-side probe is NOT inside the track-touching gate — it must run in hands-off mode too',
  );
  assertEqual(
    indexSrc.indexOf('const sendSide = await probeZoomSendSide(page);') < gateIdx,
    true,
    '[M120] and it runs before the gate, so the line is present whatever the mode',
  );
  assertEqual(
    indexSrc.includes('the earlier "AUDIO JOIN OK" line describes the RECEIVE side only'),
    true,
    '[M120] and the line disambiguates itself from the receive-side canary that misled a live diagnosis',
  );
}

// 122. W11 — the shutdown flush must not downgrade a suppressed WARNING to
//      informational on the last line before teardown.
{
  const src = readPrepareSource();
  const start = src.indexOf('export function startZoomOutboundAudioGuard');
  const fn = src.slice(start, src.indexOf('\n}\n', start));
  assertEqual(fn.includes('lastWarn = report.warn;'), true, '[M115] the guard remembers the last tick\'s level');
  assertEqual(fn.includes('if (tail) emit(tail, lastWarn);'), true, '[M115] and the shutdown flush uses it');
  assertEqual(codeOnly(fn).includes('emit(tail, false)'), false, '[M115] the hardcoded informational level is gone');
}

// 123. W6 — the element key is PREFIXED so a reader can tell an id (immune to
//      sibling insertion) from a path (not immune). The overstated
//      "independent of state" claim is gone.
{
  const src = readPrepareSource();
  assertEqual(src.includes('return parts.length > 0 ? `path:${parts.join(\'/\')}` : \'\';'), true, '[M116] a path key is prefixed path:');
  assertEqual(src.includes('if (id) return `#${id}`;'), true, '[M116] an id key is prefixed #');
  assertEqual(src.includes('STABLE ELEMENT IDENTITY, independent of state'), false, '[M116] the overstated claim is removed');
  assertEqual(src.includes('HONEST LIMIT — this is NOT "stable identity, independent of state"'), true, '[M116] and replaced with the real limit');
  assertEqual(src.includes('can be INHERITED by a different element'), true, '[M116] naming the dangerous direction');
}

// 124. Suggestion 2 + the corrected live-run order. `none` blinds the guard, so
//      it must never be recommended for a first run.
{
  const src = readPrepareSource();
  assertEqual(src.includes("LIVE-RUN ORDER: 'off' FIRST, then 'on'. NOT 'none'"), true, '[M117] the live-run order is stated and excludes none');
  assertEqual(src.includes('blinds the very'), true, '[M117] with the reason: none emits no heartbeat');
  assertEqual(src.includes("What 'none' gives up, explicitly:"), true, '[M117] and a hand-off line for what none costs');
  const sh = readZoomBotStartScript();
  assertEqual(sh.includes('ZOOM_AUDIO_LOCK=off/none'), false, '[M117] start.sh no longer suggests none for the interim run');
}

// 125. W7 — a PARTIAL pactl failure must not boot a live unmuted mic. Total
//      failure is safe (no source at all); the dangerous case is load-module
//      succeeding while set-source-mute fails.
{
  const sh = readZoomBotStartScript();
  assertEqual(sh.includes('MIC_SRC_MODULE="$(pactl load-module module-remap-source'), true, '[M118] the module index is captured so it can be unloaded by index');
  assertEqual(sh.includes('zoom_mic_state()'), true, '[M118] a state classifier is defined');
  // MATCH THE CALL, NOT THE DEFINITION. Short-circuiting the gate to a constant
  // leaves the function defined and never invoked, and that mutation survived
  // until this line — the same "identifier instead of call" mistake this file
  // records once already for revealZoomFooter.
  assertEqual(sh.includes('case "$(zoom_mic_state)" in'), true, '[M118] and it is actually CALLED to gate the outcome');
  assertEqual(codeOnly2(sh).includes('if true; then'), false, '[M118] the gate is never short-circuited to a constant');

  // [M130] EXISTENCE AND MUTE STATE ARE SEPARATE QUESTIONS. The old predicate
  // returned false for BOTH "exists and unmuted" and "does not exist", and was
  // reused as the post-unload verdict — so three materially different states
  // printed one string. Worse, the branches were INVERTED: present-and-MUTED
  // (safe) was reported as able to transmit, while present-and-UNMUTED (the one
  // dangerous state) got the reassuring "capture source removed" line.
  const code130 = codeOnly2(sh);
  for (const state of ['absent', 'muted', 'unmuted', 'unknown']) {
    assertEqual(code130.includes(`echo ${state}`), true, `[M130] the classifier can report "${state}"`);
  }
  // The three outcomes must be DISTINCT strings, or the message is information-free.
  const outcomes = [
    'capture source REMOVED; Zoom will see no microphone',
    'still present but now reports MUTED',
    'is still PRESENT and NOT muted after the unload attempt',
  ];
  assertEqual(new Set(outcomes.map((o) => code130.includes(o))).size, 1, '[M130] all three post-unload outcomes are present');
  assertEqual(outcomes.every((o) => code130.includes(o)), true, '[M130] and each is a DISTINCT message');
  assertEqual(new Set(outcomes).size, 3, '[M130] FIXTURE: the three expected strings are themselves distinct');

  // "capable of transmitting" is the privacy verdict. It must appear EXACTLY
  // ONCE in the whole script, on the dangerous branch only — the old version
  // printed it for a successful unload and for a source that was never created.
  const transmitLines = code130.split('\n').filter((l) => l.includes('capable of transmitting'));
  assertEqual(transmitLines.length, 1, '[M130] "capable of transmitting" appears exactly ONCE in the script');
  assertEqual(transmitLines[0].includes('DANGER'), true, '[M130] and only on the DANGER branch');
  assertEqual(transmitLines[0].includes('still PRESENT and NOT muted'), true, '[M130] which is the present-and-unmuted state specifically');

  // REACHABILITY, not mere presence. Renaming a `case` arm so a state falls
  // through to `*)` left every string-presence assertion above green while the
  // MIC-FIX-NOT-IN-FORCE line became unreachable — and folding `unknown` into
  // `unmuted` survived because `echo unknown` still appeared elsewhere in the
  // function. Assert the DISPATCH ARMS, not just the text.
  assertEqual(code130.includes('case "$(zoom_mic_state)" in\n    muted)'), true, '[M130] the outer dispatch has a muted) arm');
  assertEqual(/case "\$\(zoom_mic_state\)" in[\s\S]{0,400}?\n    absent\)/.test(code130), true, '[M130] and an absent) arm that actually matches the classifier output');
  assertEqual(/case "\$mute" in \[Yy\]es\) echo muted ;; \[Nn\]o\) echo unmuted ;; \*\) echo unknown ;; esac/.test(code130), true, '[M130] and an undeterminable mute state falls to an explicit unknown, never to a verdict');
  assertEqual(code130.includes('            absent)'), true, '[M130] the post-unload dispatch has its own absent) arm');
  assertEqual(code130.includes('            muted)'), true, '[M130] and a muted) arm, so a still-present-but-muted source is not called dangerous');

  // The "fix did not take" case is SAFE but must be prominent: it is the
  // likeliest way this whole change silently fails.
  assertEqual(code130.includes('MIC FIX NOT IN FORCE'), true, '[M130] a never-created source is reported as the fix not taking');
  assertEqual(
    code130.includes('The pod is SAFE'),
    true,
    '[M130] and stated as SAFE — it must not be reported as a privacy incident',
  );
  const notInForce = code130.split('\n').filter((l) => l.includes('MIC FIX NOT IN FORCE'));
  assertEqual(notInForce.length === 1 && !notInForce[0].includes('capable of transmitting'), true, '[M130] the safe case never carries the privacy verdict');
  assertEqual(sh.includes('pactl get-source-mute "$BOT_MIC_SOURCE"'), true, '[M118] via get-source-mute');
  assertEqual(sh.includes('pactl list sources'), true, '[M118] with a fallback for older pactl');
  assertEqual(sh.includes('pactl unload-module "$MIC_SRC_MODULE"'), true, '[M118] and an unverifiable mute UNLOADS the source');
  // Ordering: verify+unload must happen before node, or the browser could
  // acquire the live mic first.
  const verifyIdx = sh.indexOf('case "$(zoom_mic_state)" in');
  const nodeIdx = sh.indexOf('node /app/dist/docker.js');
  assertEqual(verifyIdx > -1 && verifyIdx < nodeIdx, true, '[M118] the verification runs BEFORE node starts');
  assertEqual(
    sh.indexOf('pactl set-source-mute "$BOT_MIC_SOURCE" 1') < verifyIdx,
    true,
    '[M118] and after the mute it is meant to confirm',
  );
}

// 126. W8 — the block used to contradict itself about which half is the
//      guarantee. By-construction is the correct half: it is what an operator
//      judges ZOOM_AUDIO_LOCK=none on.
{
  const sh = readZoomBotStartScript();
  assertEqual(sh.includes('silence is by\n# CONSTRUCTION'), true, '[M119] the script states silence is by construction');
  assertEqual(sh.includes('DEFENCE\n# IN DEPTH on top of that'), true, '[M119] and that the mutes are defence in depth');
  assertEqual(sh.includes('the only hard guarantee'), false, '[M119] the contradicting sentence is gone');
}

/**
 * The settle-poll tests need `await`, and this harness has no top-level await,
 * so they live in an async function chained from the existing async runner at
 * the end of the file. They are NOT skipped — the tail asserts they ran.
 */
let zoomPollTestsRan = false;
async function runZoomPollTests(): Promise<void> {
  zoomPollTestsRan = true;
  console.log('\n=== W17: the start.sh mic classifier, EXECUTED against a fake pactl ===');

// 127. [M131] start.sh is shell, and every earlier assertion about it has been a
//      static guard. But `zoom_mic_state` is a self-contained function whose only
//      dependency is `pactl` — so it can be EXTRACTED AND RUN against a fake
//      pactl on PATH. That is behavioural proof, not a source guard, and it is
//      what catches the two mutations that string-presence assertions missed:
//      a case arm that no longer matches, and `unknown` folded into a verdict.
//
//      If bash or awk are unavailable this FAILS loudly rather than skipping —
//      a guard that quietly stops running is worse than no guard.
{
  const sh = readZoomBotStartScript();
  const start = sh.indexOf('zoom_mic_state() {');
  const end = sh.indexOf('\n}\n', start);
  assertEqual(start > -1 && end > start, true, '[M131] the classifier function is extractable from start.sh');
  const fn = sh.slice(start, end + 3);

  const dir = mkdtempSync(join(tmpdir(), 'zoom-mic-state-'));
  /**
   * Build a fake `pactl` that answers the three subcommands from FILES.
   *
   * Files, not printf arguments: an earlier version passed the fixtures through
   * JSON.stringify into `printf '%s'`, which escaped the TABS — so awk saw one
   * field, every scenario returned `absent`, and the `absent` case PASSED FOR
   * THE WRONG REASON. That is the "control accidentally equal to the ambient
   * value" shape, caught here only because the other five scenarios failed.
   */
  const scenario = (shortSources: string, getMute: string | null, listSources: string): string => {
    writeFileSync(join(dir, 'short.txt'), shortSources);
    writeFileSync(join(dir, 'sources.txt'), listSources);
    if (getMute === null) rmSync(join(dir, 'mute.txt'), { force: true });
    else writeFileSync(join(dir, 'mute.txt'), getMute + '\n');
    writeFileSync(
      join(dir, 'pactl'),
      [
        '#!/bin/bash',
        'D="$(dirname "$0")"',
        'case "$1 $2" in',
        '  "list short")   cat "$D/short.txt" ;;',
        '  "get-source-mute"*|"get-source-mute "*) [ -f "$D/mute.txt" ] && cat "$D/mute.txt" || exit 1 ;;',
        '  "list sources") cat "$D/sources.txt" ;;',
        '  *) exit 1 ;;',
        'esac',
        '',
      ].join('\n'),
      { mode: 0o755 },
    );
    writeFileSync(
      join(dir, 'run.sh'),
      ['#!/bin/bash', 'set -euo pipefail', `export PATH=${JSON.stringify(dir)}:$PATH`, 'BOT_MIC_SOURCE=bot_mic', fn, 'zoom_mic_state', ''].join('\n'),
      { mode: 0o755 },
    );
    return execFileSync('bash', [join(dir, 'run.sh')], { encoding: 'utf8' }).trim();
  };

  // Real tabs, as `pactl list short sources` emits.
  const listed = '0\tbot_mic\tmodule-remap-source.c\ts16le 1ch 16000Hz\tRUNNING\n';
  const otherOnly = '0\tzoom_sink.monitor\tmodule-null-sink.c\ts16le 2ch 44100Hz\tIDLE\n';

  // FIXTURE CHECK: the fake must actually be reached and the tabs must survive,
  // or every scenario collapses to `absent` and the suite lies. `muted` is only
  // reachable if the source was FOUND in the tab-separated listing.
  assertEqual(scenario(listed, 'Mute: yes', ''), 'muted', '[M131] FIXTURE+CASE: source present + get-source-mute yes => muted (proves the listing parsed)');
  assertEqual(scenario(listed, 'Mute: no', ''), 'unmuted', '[M131] source present + get-source-mute no => unmuted');
  assertEqual(scenario(otherOnly, 'Mute: no', ''), 'absent', '[M131] source NOT listed => absent, NOT unmuted (the conflation that shipped)');
  assertEqual(scenario('', 'Mute: yes', ''), 'unknown', '[M131] no pactl output at all => unknown');
  assertEqual(
    scenario(listed, null, 'Source #0\n\tName: bot_mic\n\tMute: yes\n'),
    'muted',
    '[M131] get-source-mute unavailable => the list-sources fallback reads the mute',
  );
  assertEqual(
    scenario(listed, null, 'Source #0\n\tName: bot_mic\n\tMute: no\n'),
    'unmuted',
    '[M131] and reads an unmuted source through the same fallback',
  );
  assertEqual(
    scenario(listed, null, 'Source #0\n\tName: bot_mic\n'),
    'unknown',
    '[M131] a source whose mute state cannot be determined => unknown, never a verdict',
  );
  assertEqual(
    scenario(listed, null, 'Source #0\n\tName: other_src\n\tMute: no\n\tName: bot_mic\n\tMute: yes\n'),
    'muted',
    "[M131] and reads OUR source's mute line, not an earlier source's",
  );
  rmSync(dir, { recursive: true, force: true });
}

console.log('\n=== W16: the SHIPPED defaults (QA hole 6) ===');

// 126b. [M127] stepZoomMuteWatcher was tested exhaustively with INJECTED
//       configs, so every anti-oscillator property was proven — for configs
//       that never ship. Replacing the default with `{ confirmations: 1,
//       cooldownMs: 0 }` survived the whole suite while restoring the toggle
//       oscillator the state machine exists to prevent. These assertions run the
//       state machine WITH THE SHIPPED CONFIG, so they pin behaviour rather than
//       a literal.
{
  assertEqual(zoomMuteWatcherDefaultConfig.confirmations, 2, '[M127] the shipped config requires 2 confirmations');
  assertEqual(zoomMuteWatcherDefaultConfig.cooldownMs, 30_000, '[M127] and a 30s cooldown');

  // ANTI-OSCILLATOR PROPERTY 1, under the shipped config: one confident
  // 'unmuted' reading must NOT click. With confirmations: 1 a lagging label
  // produces a click on every pass — an even number of toggles leaves the bot
  // UNMUTED, the outcome this machine exists to prevent.
  const first = stepZoomMuteWatcher(zoomMuteWatcherInitialState, 'unmuted', 1_000, zoomMuteWatcherDefaultConfig);
  assertEqual(first.action, 'none', '[M127] SHIPPED: a single unmuted reading does not click');
  assertEqual(first.reason.includes('1/2'), true, '[M127] and says how many confirmations remain');
  const second = stepZoomMuteWatcher(first.state, 'unmuted', 2_000, zoomMuteWatcherDefaultConfig);
  assertEqual(second.action, 'click', '[M127] SHIPPED: the second consecutive reading does click');

  // ANTI-OSCILLATOR PROPERTY 2, under the shipped config: a second click is
  // refused inside the cooldown. With cooldownMs: 0 sustained misreading
  // produces a rapid toggle train.
  const afterClick = second.state;
  const s3 = stepZoomMuteWatcher(afterClick, 'unmuted', 2_100, zoomMuteWatcherDefaultConfig);
  const s4 = stepZoomMuteWatcher(s3.state, 'unmuted', 2_200, zoomMuteWatcherDefaultConfig);
  assertEqual(s4.action, 'none', '[M127] SHIPPED: a second click 100ms later is REFUSED');
  assertEqual(s4.reason.includes('cooldown'), true, '[M127] by the cooldown specifically');

  // THE DEPENDENCY QA FOUND: ZOOM_RETIREMENT_REQUIRED_STRIKES is justified by
  // "each strike costs a full watcher cycle (~15s poll + cooldown)". That
  // rationale rests on the cooldown, so a pinned constant was resting on an
  // unpinned one. Assert the relationship, not just the two numbers.
  assertEqual(ZOOM_RETIREMENT_REQUIRED_STRIKES, 2, '[M127] two strikes are required to retire');
  assertEqual(
    zoomMuteWatcherDefaultConfig.cooldownMs >= 10_000,
    true,
    '[M127] and the cooldown is large enough that a strike really does cost a cycle — the premise of choosing 2',
  );
  assertEqual(
    zoomMuteWatcherDefaultConfig.confirmations >= 2,
    true,
    '[M127] and a strike needs repeated confirmation before it is even attempted',
  );
}

// 126c. [M128] The shipped SETTLE-POLL budget. The deadline is what separates a
//       proven failure from a state that simply had not arrived, so its shipped
//       value is load-bearing for Critical 2.
{
  assertEqual(zoomMutePollDefaults.firstDelayMs, 500, '[M128] first re-read at 500ms');
  assertEqual(zoomMutePollDefaults.intervalMs, 500, '[M128] then every 500ms');
  assertEqual(zoomMutePollDefaults.deadlineMs, 4_000, '[M128] up to a 4s budget');
  // The property that matters: the shipped budget must OUTLAST the single
  // +750ms sample that caused the defect, by a real margin.
  assertEqual(zoomMutePollDefaults.deadlineMs > 750, true, '[M128] the budget outlasts the old 750ms single sample');
  assertEqual(
    zoomMutePollDefaults.deadlineMs >= 4 * zoomMutePollDefaults.intervalMs,
    true,
    '[M128] and allows at least four re-reads, so one slow repaint is not a proven failure',
  );
}

// 126d. Narrow source guard: the watcher's default parameter must BE the named
//       const, not an inline copy that could drift from it.
{
  const src = readPrepareSource();
  assertEqual(
    src.includes('config: ZoomMuteWatcherConfig = zoomMuteWatcherDefaultConfig,'),
    true,
    '[M127] the watcher default parameter IS the named shipped config',
  );
  assertEqual(
    codeOnly(src).includes('ZoomMuteWatcherConfig = { confirmations:'),
    false,
    '[M127] and is not an inline literal that could drift from the pinned one',
  );
}

console.log('\n=== W13: the post-click SETTLE POLL (Critical 2) ===');

  /**
   * Fake clock + sleep. `sleep` advances the clock, so the poll's timing is
   * exercised deterministically and the numbers live in FIXTURES rather than only
   * in the source — the whole point, since `750` previously appeared exactly once
   * in the tree, in the code, and in no test at all.
   */
  function fakeClock(): { now: () => number; sleep: (ms: number) => Promise<void>; set: (v: number) => void } {
    let t = 0;
    return { now: () => t, sleep: async (ms: number) => { t += ms; }, set: (v: number) => { t = v; } };
  }

  const pollKey = 'path:div:2/div:0/button:0';
  /** A prober whose element reads `unmuted` until `settleAtMs`, then `muted`. */
  function settlingProber(clock: { now: () => number }, settleAtMs: number): () => Promise<ZoomMicCandidate[]> {
    return async () => [
      {
        selector: 'button.join-audio-container__btn',
        index: 0,
        probe: probe({
          elementKey: pollKey,
          labelText: clock.now() >= settleAtMs ? 'Unmute' : 'Mute',
        }),
      },
    ];
  }

  // 110. [M103] THE DEFECT. Zoom's mute is an audio-session operation acked by the
  //      server, not a local CSS toggle, so on a loaded pod the toolbar reflects it
  //      LATER than the old single +750ms sample. Here it settles at 1200ms.
  {
    const clock = fakeClock();
    const probeOnce = settlingProber(clock, 1200);

    // FIXTURE HAZARD CHECK — prove the old strategy really does fail on this
    // fixture, so the test below cannot pass vacuously. A single read at +750ms
    // is exactly a poll whose first delay IS the whole budget.
    const single = await pollZoomMuteSettled(probeOnce, pollKey, { firstDelayMs: 750, intervalMs: 750, deadlineMs: 750 }, clock.now, clock.sleep);
    assertEqual(single.polls, 1, '[M103] FIXTURE: the old strategy takes exactly ONE reading');
    assertEqual(single.after !== null && single.after.reading.kind, 'unmuted', '[M103] FIXTURE: ...and at +750ms it samples the PRE-CLICK state');
    assertEqual(single.timedOut, true, '[M103] FIXTURE: which it would have called a proven failure');
    const wouldHaveRetired = reportZoomMuteClick({
      reason: 'r', before: { candidate: { selector: 's', index: 0, probe: probe({ elementKey: pollKey }) }, reading: { kind: 'unmuted', evidence: 'e' } },
      after: single.after, afterDetail: 'd', priorIneffectiveClicks: 0,
    });
    assertEqual(wouldHaveRetired.verdict, 'still-unmuted', '[M103] FIXTURE: the old strategy yields a PROVEN-FAILURE verdict');
    assertEqual(wouldHaveRetired.rejectKey, pollKey, '[M103] FIXTURE: ...which would have retired the control that WORKS');

    // Now the poll, with the shipped budget.
    const clock2 = fakeClock();
    const r = await pollZoomMuteSettled(settlingProber(clock2, 1200), pollKey, zoomMutePollDefaults, clock2.now, clock2.sleep);
    assertEqual(r.timedOut, false, '[M103] the poll does NOT time out — the state arrived');
    assertEqual(r.after !== null && r.after.reading.kind, 'muted', '[M103] and the settled reading is muted');
    assertEqual(r.settledAtMs, 1500, '[M103] observed at 1500ms (500 first delay + two 500ms gaps)');
    assertEqual(r.polls, 3, '[M103] after 3 probes');
    const good = reportZoomMuteClick({
      reason: 'r', before: { candidate: { selector: 's', index: 0, probe: probe({ elementKey: pollKey }) }, reading: { kind: 'unmuted', evidence: 'e' } },
      after: r.after, afterDetail: 'd', priorIneffectiveClicks: 0,
    });
    assertEqual(good.verdict, 'muted', '[M103] so the verdict is muted, not still-unmuted');
    assertEqual(good.rejectKey, null, '[M103] and the working control is NOT retired');
  }

  // 111. [M104] The budget is a real boundary: shorten it below the settle time and
  //      the verdict becomes a proven failure again. This is the same fixture with
  //      one number changed, which is what makes the poll's budget load-bearing
  //      rather than decorative.
  {
    const clock = fakeClock();
    const r = await pollZoomMuteSettled(settlingProber(clock, 1200), pollKey, { firstDelayMs: 500, intervalMs: 500, deadlineMs: 700 }, clock.now, clock.sleep);
    assertEqual(r.timedOut, true, '[M104] a budget shorter than the settle time times out');
    assertEqual(r.after !== null && r.after.reading.kind, 'unmuted', '[M104] and the last reading is the pre-click state');
    assertEqual(r.settledAtMs, null, '[M104] with no settle time recorded');
  }

  // 112. [M105] Early exit: an element already muted on the FIRST probe must not
  //      burn the whole budget.
  {
    const clock = fakeClock();
    const r = await pollZoomMuteSettled(settlingProber(clock, 0), pollKey, zoomMutePollDefaults, clock.now, clock.sleep);
    assertEqual(r.polls, 1, '[M105] one probe is enough when the state is already there');
    assertEqual(r.settledAtMs, 500, '[M105] observed at the first delay');
    assertEqual(r.timedOut, false, '[M105] no timeout');
  }

  // 113. [M106] The poll reads ONLY the clicked element (composes with Critical 1).
  //      A decoy that reads muted the whole time must never satisfy the poll.
  {
    const clock = fakeClock();
    const decoyOnly = async (): Promise<ZoomMicCandidate[]> => [
      { selector: 'footer button[aria-label*="audio" i]', index: 0, probe: probe({ elementKey: 'path:div:9/button:9', ariaLabel: 'Unmute' }) },
    ];
    assertEqual(
      selectZoomMicToggle(await decoyOnly()) !== null,
      true,
      '[M106] FIXTURE: the decoy DOES read confidently, so a fresh selection would have taken it',
    );
    const r = await pollZoomMuteSettled(decoyOnly, pollKey, { firstDelayMs: 100, intervalMs: 100, deadlineMs: 300 }, clock.now, clock.sleep);
    assertEqual(r.after, null, '[M106] the clicked element was never present, so there is NO reading');
    assertEqual(r.timedOut, true, '[M106] the poll runs its budget rather than accepting the decoy');
    assertEqual(r.polls >= 2, true, '[M106] and it really did poll more than once');
  }

  // 113b. [M104b] HARD ITERATION CAP. The deadline is the normal exit; the cap is
  //       the backstop for a clock that does not advance. This loop runs inside a
  //       setInterval callback in a live bot, so no edit may make it unbounded —
  //       and a mutation that disabled the deadline check DID hang the suite
  //       rather than failing it, which is how this cap was found.
  {
    // The fake sleep yields a MACROTASK deliberately. A synchronous
    // `async () => {}` sleep starves the timer queue, so a non-terminating poll
    // would hang the whole suite instead of failing it — which is exactly what
    // happened when the cap was mutated away. A test that asserts termination
    // must be able to OBSERVE non-termination.
    const frozen = {
      now: () => 0, // clock never advances, so the deadline can never fire
      sleep: (_ms: number) => new Promise<void>((res) => setTimeout(res, 0)),
    };
    const raced = await Promise.race([
      pollZoomMuteSettled(
        async () => [{ selector: 's', index: 0, probe: probe({ elementKey: pollKey, ariaLabel: 'Mute' }) }],
        pollKey,
        { firstDelayMs: 500, intervalMs: 500, deadlineMs: 4_000 },
        frozen.now,
        frozen.sleep,
      ),
      new Promise<'DID-NOT-TERMINATE'>((res) => setTimeout(() => res('DID-NOT-TERMINATE'), 3_000)),
    ]);
    assertEqual(raced !== 'DID-NOT-TERMINATE', true, '[M104b] the poll TERMINATES even when the deadline can never fire');
    if (raced !== 'DID-NOT-TERMINATE') {
      assertEqual(raced.timedOut, true, '[M104b] and reports a timeout');
      assertEqual(raced.polls, 10, '[M104b] after the computed cap (ceil(4000/500)+2), not forever');
      assertEqual(raced.after !== null && raced.after.reading.kind, 'unmuted', '[M104b] with the last reading still reported');
    }
  }

  // 114. [M107] A confident reading seen mid-poll is not erased by a later absence
  //      (the toolbar can auto-hide between probes).
  {
    const clock = fakeClock();
    let call = 0;
    const flaky = async (): Promise<ZoomMicCandidate[]> => {
      call++;
      if (call === 1) return [{ selector: 's', index: 0, probe: probe({ elementKey: pollKey, labelText: 'Mute' }) }];
      return []; // toolbar vanished
    };
    const r = await pollZoomMuteSettled(flaky, pollKey, { firstDelayMs: 100, intervalMs: 100, deadlineMs: 300 }, clock.now, clock.sleep);
    assertEqual(r.after !== null && r.after.reading.kind, 'unmuted', '[M107] the earlier confident reading is retained');
    assertEqual(r.timedOut, true, '[M107] and the budget still expires honestly');
  }


}

console.log('\n=== LAYER 2: getUserMedia patch (async) ===');

// 41. [M25] getUserMedia: a mic track acquired later is silenced at birth.
async function runAsyncTests(): Promise<void> {
  const acquired = makeTrack('audio');
  const stream = { getAudioTracks: () => [acquired.track] };
  let patchedFn: ((c: unknown) => Promise<unknown>) | null = null;
  const mediaDevices = { getUserMedia: (_c: unknown) => Promise.resolve(stream) };

  const g = globalThis as unknown as Record<string, unknown>;
  const savedNav = Object.getOwnPropertyDescriptor(g, 'navigator');
  const savedLock = Object.getOwnPropertyDescriptor(g, '__vexa_zoom_audio_lock');
  delete g.__vexa_zoom_audio_lock;
  Object.defineProperty(g, 'navigator', { value: { mediaDevices }, writable: true, configurable: true });
  try {
    const r = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    assertEqual(r.patchedGetUserMedia, true, '[M25] getUserMedia was patched');
    patchedFn = mediaDevices.getUserMedia as (c: unknown) => Promise<unknown>;
    const returned = await patchedFn({ audio: true });
    assertEqual(returned === stream, true, '[M25] the original stream is still returned to the caller');
    assertEqual(acquired.real(), false, '[M25] ZERO-WINDOW: mic track from getUserMedia is disabled on acquisition');
    acquired.track.enabled = true;
    assertEqual(acquired.real(), false, '[M25] the acquired track is also sealed against unmuting');
  } finally {
    delete g.__vexa_zoom_audio_lock;
    if (savedLock) Object.defineProperty(g, '__vexa_zoom_audio_lock', savedLock);
    if (savedNav) Object.defineProperty(g, 'navigator', savedNav);
    else delete g.navigator;
  }

  // 41b. [M31] A getUserMedia stream can carry BOTH kinds. Only the audio track
  //      may be touched — a video track in the same stream must pass through
  //      untouched, and a stream with no audio at all must be returned intact.
  //      Guards the constraint that nothing here perturbs anything but outbound
  //      audio.
  const mixedAudio = makeTrack('audio');
  const mixedVideo = makeTrack('video');
  const mixedStream = {
    getAudioTracks: () => [mixedAudio.track],
    getVideoTracks: () => [mixedVideo.track],
  };
  const videoOnly = { getAudioTracks: () => [] };
  const savedNav2 = Object.getOwnPropertyDescriptor(g, 'navigator');
  const savedLock2 = Object.getOwnPropertyDescriptor(g, '__vexa_zoom_audio_lock');
  delete g.__vexa_zoom_audio_lock;
  let handed: unknown = null;
  const md2 = { getUserMedia: (_c: unknown) => Promise.resolve(handed) };
  Object.defineProperty(g, 'navigator', { value: { mediaDevices: md2 }, writable: true, configurable: true });
  try {
    installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    const fn2 = md2.getUserMedia as (c: unknown) => Promise<unknown>;

    handed = mixedStream;
    const outMixed = await fn2({ audio: true, video: true });
    assertEqual(mixedAudio.real(), false, '[M31] audio track in a mixed stream is disabled');
    assertEqual(mixedVideo.real(), true, '[M31] VIDEO track in the same stream is left untouched');
    assertEqual(outMixed === mixedStream, true, '[M31] the mixed stream is returned intact');

    handed = videoOnly;
    const outVideoOnly = await fn2({ video: true });
    assertEqual(outVideoOnly === videoOnly, true, '[M31] a stream with no audio tracks passes through unchanged');
  } finally {
    delete g.__vexa_zoom_audio_lock;
    if (savedLock) Object.defineProperty(g, '__vexa_zoom_audio_lock', savedLock);
    if (savedNav) Object.defineProperty(g, 'navigator', savedNav);
    else delete g.navigator;
  }
}

console.log('\n=== W11: the 2026-09-02 live digest — a MUTED bot that read "unmuted" ===');

/**
 * THE PRODUCER'S OWN OUTPUT, transcribed from the live run.
 *
 * Container: image zoom-bot:v0.1.14, real Zoom meeting, 2026-09-02 11:06–11:10.
 * The meeting SUCCEEDED and the user visually confirmed the participant list
 * showed the bot as MUTED. Container-side (`bot mic VERIFIED muted at the
 * capture level (source=bot_mic)`) and page-side (`blockedUnmutes=1`,
 * `sweepAlreadyDisabled=1`) agreed. The DOM reader disagreed with all of it:
 *
 *   button.join-audio-container__btn[0]
 *     key=path:div:2/div:4/div:0/div:0/div:0/div:0/div:0/footer:2/div:0/div:0/div:0/button:0
 *     aria-label="audio"  label="Audio"  aria-pressed=absent
 *     -> unmuted (class hint "svgaudiounmuted" [caret: yes])
 *
 *   footer button[aria-label*="audio" i][1]
 *     key=path:div:0/div:0/div:0/div:0/footer:2/div:0/div:0/div:0/div:1/div:0/div:0/button:0
 *     aria-label="More audio controls"  label=""  aria-pressed=absent
 *     -> not-mute-toggle (aria-label "more audio controls" unrecognised [caret: yes])
 *
 * WHAT IS TRANSCRIBED AND WHAT IS INFERRED, stated so nobody mistakes one for
 * the other:
 *   - transcribed verbatim: the selector, the index, the elementKey, aria-label,
 *     label, `aria-pressed=absent`, the evidence tier, and `[caret: yes]`.
 *   - inferred from the digest, and each inference is asserted below rather
 *     than assumed: the unmuted glyph was NOT among the RENDERED descendants
 *     (otherwise the precise whitelist tier would have fired and the evidence
 *     would read "visible icon class", not "class hint"); and the raw class
 *     attribute casing is unrecoverable from the evidence string, which prints
 *     the normalised list entry — 'SvgAudioUnmuted' is used because that is the
 *     spelling Zoom ships, and normalisation makes the casing irrelevant.
 *   - NOT reported by the digest, so left absent: the element's own textContent
 *     (`label=` is the label NODE) and its class attribute.
 */
const live0902_audioBtnKey = 'path:div:2/div:4/div:0/div:0/div:0/div:0/div:0/footer:2/div:0/div:0/div:0/button:0';
const live0902_moreBtnKey = 'path:div:0/div:0/div:0/div:0/footer:2/div:0/div:0/div:0/div:1/div:0/div:0/button:0';

const live0902_audioBtn: Partial<ZoomMicProbe> = {
  ariaLabel: 'audio',
  labelText: 'Audio',
  ariaPressed: null,
  className: 'footer-button-base__button ax-outline join-audio-container__btn',
  descendantClassNames: ['zm-icon SvgAudioUnmuted'],
  visibleDescendantClassNames: [],
  caretNearby: true,
  elementKey: live0902_audioBtnKey,
};

const live0902_moreBtn: Partial<ZoomMicProbe> = {
  ariaLabel: 'More audio controls',
  labelText: '',
  ariaPressed: null,
  caretNearby: true,
  elementKey: live0902_moreBtnKey,
};

// The digest listed FOUR rows: three selectors matching the same button, plus
// the "More audio controls" sibling. Reproduced at the same indices, because
// "the same element under several selector names" is the shape that made a
// selector-keyed rejection useless (M59).
const live0902Candidates: ZoomMicCandidate[] = [
  { selector: 'button.join-audio-container__btn', index: 0, probe: probe(live0902_audioBtn) },
  { selector: 'button[class*="join-audio-container" i]', index: 0, probe: probe(live0902_audioBtn) },
  { selector: 'footer button[aria-label*="audio" i]', index: 0, probe: probe(live0902_audioBtn) },
  { selector: 'footer button[aria-label*="audio" i]', index: 1, probe: probe(live0902_moreBtn) },
];

// 109. [W11-HAZARD] FIRST, prove the hazard was really present in this input —
//      otherwise the assertions below could be passing for the wrong reason (a
//      fixture the producer never emits, or one no tier reads at all).
{
  const p = probe(live0902_audioBtn);
  const hintTokens = p.descendantClassNames.join(' ').toLowerCase();
  assertEqual(
    zoomMicUnmutedClassHints.some((h) => hintTokens.includes(h)),
    true,
    '[W11-HAZARD] the fixture DOES carry an unmuted-directed class hint — the weak tier was reachable',
  );
  const visibleTokens = p.visibleDescendantClassNames.join(' ').toLowerCase();
  assertEqual(
    [...zoomMicIconUnmutedClasses, ...zoomMicIconMutedClasses].some((c) => visibleTokens.includes(c)),
    false,
    '[W11-HAZARD] and NO precise glyph was rendered — nothing could outrank that hint',
  );
  // Defuse the new guard (caret absent) and the old, dangerous path is still
  // exactly what this input reaches: the weakest tier, pointing at unmuted.
  const guardOff = readZoomMicState(probe({ ...live0902_audioBtn, caretNearby: false }));
  assertEqual(guardOff.kind, 'unmuted-unconfirmed', '[W11-HAZARD] with the options guard defused it still reaches the WEAK tier');
  assertEqual(
    guardOff.evidence.includes('class hint "svgaudiounmuted"'),
    true,
    '[W11-HAZARD] on precisely the hint the live run reported — this is the reading that shipped as "unmuted"',
  );
}

// 110. [W11] THE FIXTURE THAT IS THE POINT OF THIS CHANGE. Every row of the live
//      digest must classify NON-ACTIONABLE.
{
  const kinds = live0902Candidates.map((c) => readZoomMicState(c.probe).kind);
  assertEqual(kinds, ['not-mute-toggle', 'not-mute-toggle', 'not-mute-toggle', 'not-mute-toggle'], '[W11] all four live rows read not-mute-toggle');
  assertEqual(kinds.includes('unmuted'), false, '[W11] and NOT ONE of them reads "unmuted" — the reading that triggers a click');

  const audio = readZoomMicState(probe(live0902_audioBtn));
  assertEqual(audio.evidence.includes('AUDIO OPTIONS control'), true, '[W11] the "Audio"/caret control is POSITIVELY identified, not left as "unrecognised"');
  const more = readZoomMicState(probe(live0902_moreBtn));
  assertEqual(more.evidence.includes('AUDIO OPTIONS control'), true, '[W11] and so is the "More audio controls" sibling');
}

// 111. [W11] No selection => no click. This is the assertion that the four
//      ineffective clicks of the live run cannot happen again.
{
  assertEqual(selectZoomMicToggle(live0902Candidates), null, '[W11] the live candidate set yields NO selection, so nothing is clicked');
  const step = stepZoomMuteWatcher(zoomMuteWatcherInitialState, 'none', 1_000, zoomMuteWatcherDefaultConfig);
  assertEqual(step.action, 'none', '[W11] and the watcher step takes no action on "none"');
  assertEqual(step.state.clicks, 0, '[W11] with the click counter untouched');
  // Belt and braces: even if a selection were somehow produced, not-mute-toggle
  // is not an actionable reading.
  assertEqual(
    stepZoomMuteWatcher(zoomMuteWatcherInitialState, 'not-mute-toggle', 1_000, zoomMuteWatcherDefaultConfig).action,
    'none',
    '[W11] a not-mute-toggle reading is never actionable either',
  );
  // THE DEMOTED READING'S DISPATCH, driven PAST the confirmation threshold.
  //
  // A single step here proves nothing and is the vacuous shape this codebase
  // keeps producing: with confirmations=2, the FIRST 'unmuted' step also
  // returns action 'none', so a one-step assertion passes whether or not the
  // demoted kind is treated as actionable. It has to be driven far enough that
  // a genuine 'unmuted' would have clicked — and that CONTROL is asserted
  // first, so the ambient value cannot be mistaken for the result.
  {
    const cfg = zoomMuteWatcherDefaultConfig;
    const drive = (reading: Parameters<typeof stepZoomMuteWatcher>[1]) => {
      let st = zoomMuteWatcherInitialState;
      let clicks = 0;
      for (let i = 0; i < cfg.confirmations + 3; i++) {
        const step = stepZoomMuteWatcher(st, reading, 1_000 + i * (cfg.cooldownMs + 1_000), cfg);
        st = step.state;
        if (step.action === 'click') clicks++;
      }
      return clicks;
    };
    assertEqual(drive('unmuted') > 0, true, '[W11] CONTROL: a genuine "unmuted" DOES click once driven past the confirmation threshold');
    assertEqual(drive('unmuted-unconfirmed'), 0, '[W11] but "unmuted-unconfirmed" NEVER clicks, however many passes it is driven for');
    assertEqual(drive('not-mute-toggle'), 0, '[W11] and neither does "not-mute-toggle"');
    assertEqual(drive('unknown'), 0, '[W11] nor "unknown"');
  }

  // ...and the demoted reading must not be SELECTABLE either. The live rows are
  // rejected one tier earlier by the options guard, so without this fixture the
  // demotion's own dispatch through selectZoomMicToggle is never exercised.
  {
    const hintOnly = candidate('button.join-audio-container__btn', 0, { descendantClassNames: ['zm-icon SvgAudioUnmuted'], elementKey: 'k-hint' });
    assertEqual(readZoomMicState(hintOnly.probe).kind, 'unmuted-unconfirmed', '[W11] FIXTURE: a hint-only candidate really does read unmuted-unconfirmed');
    assertEqual(selectZoomMicToggle([hintOnly]), null, '[W11] and it is NOT selectable, so no click can reach it');
    // CONTROL: the same element with the glyph RENDERED is selectable, so the
    // assertion above is about the demotion and not about selection being broken.
    const rendered = candidate('button.join-audio-container__btn', 0, { visibleDescendantClassNames: ['zm-icon SvgAudioUnmuted'], elementKey: 'k-vis' });
    assertEqual(selectZoomMicToggle([rendered])?.reading.kind, 'unmuted', '[W11] CONTROL: the same element with a RENDERED glyph IS selected');
  }
}

// 112. [W11] REQUIREMENT 4: the retirement machinery must not be exercised at
//      all on this DOM. A reset every ~60s for a healthy, muted bot is noise
//      that would mask a real problem.
{
  assertEqual(
    zoomRetirementIsAbsorbing(live0902Candidates, new Set()),
    false,
    '[W11] with nothing retired, the absorbing check is false — no reset',
  );
  // The stronger property: even with a retirement set, there is no CONFIDENT
  // candidate to be absorbed by, so the reset can never fire on this DOM.
  assertEqual(
    zoomRetirementIsAbsorbing(live0902Candidates, new Set([live0902_audioBtnKey, live0902_moreBtnKey])),
    false,
    '[W11] and no confidently-readable candidate exists, so it stays false even with keys retired',
  );
  // CONTROL — the check is not simply always false. A confident candidate that
  // IS retired does make it absorbing, which is how the live run reached two
  // resets. Without this control the assertions above are vacuous.
  assertEqual(
    zoomRetirementIsAbsorbing([{ selector: 's', index: 0, probe: probe({ ariaLabel: 'Mute', elementKey: 'k' }) }], new Set(['k'])),
    true,
    '[W11] CONTROL: a retired CONFIDENT candidate does make it absorbing (so the check can return true)',
  );

  // The other half of "the machinery is not exercised": a STRIKE can only come
  // from a click's post-click report, so a pass that clicks nothing cannot
  // strike anything. That is structural — setInterval + Playwright — so it is a
  // SOURCE guard, and weaker than the behavioural assertions above.
  const fn = (() => {
    const src = readPrepareSource();
    const start = src.indexOf('export function startZoomMuteWatcher');
    return src.slice(start, src.indexOf('\n}\n', start));
  })();
  const clickBranch = "if (step.action === 'click' && selection) {";
  assertEqual(fn.includes(clickBranch), true, '[W11] the watcher gates its whole click path on step.action');
  assertEqual(
    fn.indexOf('stepZoomRetirement(retirement, report.rejectKey)') > fn.indexOf(clickBranch),
    true,
    '[W11] and retirement is stepped only INSIDE that branch — no click, no strike, no reset',
  );
  assertEqual(
    codeOnly(fn).split('stepZoomRetirement(').length - 1,
    1,
    '[W11] with exactly ONE call site, so the guard above covers every path to a strike',
  );
}

console.log('\n=== W11: isZoomAudioOptionsControl — five conditions, each load-bearing ===');

// 113. [W11] The base case is the live element, so every refusal below is a
//      one-field delta from a fixture that really fires.
assertEqual(isZoomAudioOptionsControl(probe(live0902_audioBtn)), true, '[W11] BASE: the live "Audio"/caret element is the audio-options control');
assertEqual(isZoomAudioOptionsControl(probe(live0902_moreBtn)), true, '[W11] BASE: so is "More audio controls" with an empty visible label');

// 114. [W11] Condition 1 — caret PRESENCE, never absence. Absence is the M30
//      blindness hazard: if these selectors stop matching, a presence-keyed rule
//      stops REJECTING (safe) whereas an absence-keyed one vetoes everything.
assertEqual(isZoomAudioOptionsControl(probe({ ...live0902_audioBtn, caretNearby: false })), false, '[W11] no caret => not the options control');
assertEqual(isZoomAudioOptionsControl(probe({ ...live0902_audioBtn, caretNearby: null })), false, '[W11] caret NOT PROBED is not a yes either');

// 115. [W11] Condition 2/3 — WHOLE-LABEL equality. This is the over-matching
//      guard: "audio" is a substring of real state labels, and rejecting on the
//      substring would silence the reader on exactly the labels it must read.
{
  for (const [label, expectedKind] of [['unmute my audio', 'muted'], ['mute audio', 'unmuted'], ['mute my audio', 'unmuted']] as const) {
    assertEqual(isZoomAudioOptionsControl(probe({ ...live0902_audioBtn, ariaLabel: label, labelText: null })), false, `[W11] "${label}" contains "audio" but is NOT the options control`);
    assertEqual(readZoomMicState(probe({ ...live0902_audioBtn, ariaLabel: label, labelText: null })).kind, expectedKind, `[W11] and "${label}" still reads ${expectedKind}`);
  }
  assertEqual(isZoomAudioOptionsControl(probe({ ...live0902_audioBtn, ariaLabel: 'Reactions', labelText: null })), false, '[W11] an unrelated label is not the options control');
  assertEqual(isZoomAudioOptionsControl(probe({ caretNearby: true })), false, '[W11] a caret with NO label at all rejects nothing — no positive evidence');
  // Mixed: one field generic, the other saying something real. The real one wins.
  assertEqual(isZoomAudioOptionsControl(probe({ ...live0902_audioBtn, labelText: 'Mute' })), false, '[W11] aria-label "audio" + label node "Mute" => NOT the options control');
  assertEqual(readZoomMicState(probe({ ...live0902_audioBtn, labelText: 'Mute' })).kind, 'unmuted', '[W11] and that element is read as unmuted, on the label node');
  assertEqual(readZoomMicState(probe({ ...live0902_audioBtn, labelText: 'Unmute' })).kind, 'muted', '[W11] and "Unmute" as muted, with the right polarity');
}

// 116. [W11] Condition 4/5 — the guard DEFERS to every stronger signal. This is
//      what makes its position in readZoomMicState unable to change a verdict,
//      which kills the "someone reorders the branches" mutation class instead of
//      pinning one order.
{
  assertEqual(isZoomAudioOptionsControl(probe({ ...live0902_audioBtn, ariaPressed: 'true' })), false, '[W11] aria-pressed="true" outranks the guard');
  assertEqual(readZoomMicState(probe({ ...live0902_audioBtn, ariaPressed: 'true' })).kind, 'muted', '[W11] and that element reads muted');

  const withVisibleUnmuted = { ...live0902_audioBtn, visibleDescendantClassNames: ['zm-icon SvgAudioUnmuted'] };
  assertEqual(isZoomAudioOptionsControl(probe(withVisibleUnmuted)), false, '[W11] a RENDERED unmuted glyph outranks the guard');
  assertEqual(readZoomMicState(probe(withVisibleUnmuted)).kind, 'unmuted', '[W11] and the precise tier reads it as unmuted — still clickable');

  const withVisibleMuted = { ...live0902_audioBtn, visibleDescendantClassNames: ['zm-icon SvgAudioMuted'] };
  assertEqual(isZoomAudioOptionsControl(probe(withVisibleMuted)), false, '[W11] a RENDERED muted glyph outranks it too');
  assertEqual(readZoomMicState(probe(withVisibleMuted)).kind, 'muted', '[W11] and reads muted — the join-time "already muted" path is intact');

  const withHeadset = { ...live0902_audioBtn, visibleDescendantClassNames: ['zm-icon SvgJoinAudio'] };
  assertEqual(isZoomAudioOptionsControl(probe(withHeadset)), false, '[W11] and so does the UNJOINED glyph');
  assertEqual(
    readZoomMicState(probe(withHeadset)).evidence.includes('UNJOINED audio control'),
    true,
    '[W11] so the more specific "unjoined" evidence survives instead of being swallowed by the guard',
  );

  // Non-mic vocabulary is checked BEFORE the guard and must stay that way: a
  // "Mute All" verdict is the one whose consequence is muting every human.
  assertEqual(readZoomMicState(probe({ ariaLabel: 'Mute All', caretNearby: true })).kind, 'not-mic-control', '[W11] "Mute All" is still not-mic-control, caret or no caret');
}

// 117. [W11] SPELLING LOCK on the vocabulary list. Adding 'mute audio' here
//      would make the guard swallow a real state label, so the list is asserted
//      to carry no mute vocabulary at all.
{
  assertEqual(zoomAudioOptionsExactLabels.length > 0, true, '[W11] the audio-options vocabulary is non-empty (an empty list would silently disable the guard)');
  assertEqual(zoomAudioOptionsExactLabels.includes('audio'), true, '[W11] and contains the live spelling "audio"');
  assertEqual(zoomAudioOptionsExactLabels.includes('more audio controls'), true, '[W11] and the live sibling spelling "more audio controls"');
  for (const entry of zoomAudioOptionsExactLabels) {
    assertEqual(readZoomMuteVocabulary(entry), null, `[W11] "${entry}" carries no mute vocabulary — a generic label may never swallow a state word`);
    assertEqual(entry, normaliseZoomMicText(entry), `[W11] "${entry}" is already normalised, so whole-label equality can match it`);
  }
}

// 118. [W11] NON-REGRESSION: the 2026-09-01 shape. `aria-label="audio"` was a
//      deliberately-supported shape and must NOT be blacklisted. With no caret
//      probed, the guard cannot fire and the element falls through to the state
//      tiers exactly as it did before.
{
  const nineteen = probe({ ariaLabel: 'audio', className: 'footer-button-base__button ax-outline join-audio-container__btn' });
  assertEqual(isZoomAudioOptionsControl(nineteen), false, '[W11] the 2026-09-01 shape is NOT matched by the options guard');
  const r = readZoomMicState(nineteen);
  assertEqual(r.kind, 'not-mute-toggle', '[W11] it still reads not-mute-toggle (unchanged)');
  assertEqual(r.evidence.includes('AUDIO OPTIONS control'), false, '[W11] and NOT via the options guard — the tier that decided it is unchanged');
  assertEqual(r.evidence.includes('carries no mute state'), true, '[W11] it is still the known-non-state-label tier that decides it');
  // ...and the same element with a real state signal is still readable.
  assertEqual(readZoomMicState(probe({ ariaLabel: 'audio', ariaPressed: 'true' })).kind, 'muted', '[W11] aria-label="audio" + aria-pressed="true" => muted, as before');
}

// 119. [W11] NON-REGRESSION: the state-word readings. "Muted"/"You are muted"
//      are STATE, not an offered action, and a caret must not change that.
{
  for (const label of ['Muted', 'You are muted', 'muted']) {
    assertEqual(readZoomMicState(probe({ ariaLabel: label, caretNearby: true })).kind, 'muted', `[W11] "${label}" => muted, caret present`);
  }
  assertEqual(readZoomMicState(probe({ ariaLabel: 'Mute', caretNearby: true })).kind, 'unmuted', '[W11] "Mute" (action offered) => unmuted, caret present');
  assertEqual(readZoomMicState(probe({ ariaLabel: 'Unmute', caretNearby: true })).kind, 'muted', '[W11] "Unmute" (action offered) => muted, caret present');
}

console.log('\n=== W11: the overstated warnings (the 9th and 10th) ===');

// 120. [W11] The layer-1 WARNING asserted "the bot may APPEAR unmuted to
//      participants" / "the participant list may show this bot as unmuted".
//      Both are claims about the ROOM, and both were FALSE on 2026-09-02 while
//      firing. The lines may now say only that the DOM could not be read.
{
  const idx = codeOnly(readZoomWebSource('index.ts'));
  assertEqual(idx.includes('may APPEAR unmuted to participants'), false, '[W11] index.ts no longer claims the bot may appear unmuted');
  assertEqual(idx.includes('DOM mute STATE could not be read'), true, '[W11] it states only that the STATE was unreadable');
  assertEqual(idx.includes('NOT evidence that the bot appears unmuted'), true, '[W11] and explicitly disclaims the inference');
  assertEqual(idx.includes('participant list'), true, '[W11] pointing at the authority on APPEARANCE');
  assertEqual(idx.includes('outbound-audio guard heartbeat'), true, '[W11] and at the authority on SILENCE');

  const prep = codeOnly(readPrepareSource());
  assertEqual(prep.includes('the participant list may show this bot as unmuted'), false, '[W11] prepare.ts drops the same claim');
  assertEqual(prep.includes('could not read the in-meeting mute STATE from the DOM'), true, '[W11] and states only what it observed');
  assertEqual(prep.includes('NOT that the bot appears unmuted'), true, '[W11] with the inference disclaimed there too');
}

// 121. [W11] The selectors.ts note said "if the bot appeared unmuted, fix these
//      selectors". On 2026-09-02 the selectors matched, the reader said unmuted,
//      and the room saw MUTED — so that inference is not available either.
{
  const sel = readZoomWebSource('selectors.ts');
  assertEqual(sel.includes('If you are here because the bot appeared unmuted, fix these'), false, '[W11] the selectors.ts note drops the unsupported inference');
  assertEqual(sel.includes('CORRECTED 2026-09-02'), true, '[W11] and records why');
}


runAsyncTests()
  .then(runZoomPollTests)
  .then(() => {
    // A suite that silently stops running is worse than no suite. Assert the
    // awaited block actually executed rather than trusting the chain.
    assertEqual(zoomPollTestsRan, true, 'the settle-poll suite RAN (not silently skipped)');
  })
  .catch((e) => {
    failed++;
    console.log(`  ❌ async suite threw: ${e?.message ?? e}`);
  })
  .then(() => {
    console.log(`\n${failed === 0 ? '✅' : '❌'} mute-guarantee: ${passed} passed, ${failed} failed\n`);
    process.exit(failed === 0 ? 0 : 1);
  });
