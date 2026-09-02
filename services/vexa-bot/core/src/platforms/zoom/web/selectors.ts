// Zoom Web Client (browser-based) selectors — verified from live DOM inspection
// Navigate to: https://app.zoom.us/wc/MEETING_ID/join?pwd=PASSWORD

// ---- Pre-join page ----

// Name input: <input id="input-for-name">
export const zoomNameInputSelector = '#input-for-name';

// Join button: <button class="zm-btn preview-join-button ..."> — disabled until name entered
export const zoomJoinButtonSelector = 'button.preview-join-button';

// Mute button in preview: <button id="preview-audio-control-button" aria-label="Mute">
export const zoomPreviewMuteSelector = '#preview-audio-control-button';

// Stop Video button in preview: <button id="preview-video-control-button" aria-label="Stop Video">
export const zoomPreviewVideoSelector = '#preview-video-control-button';

// Permission dialog (React portal): shown twice — once for camera+mic, once for mic only
// Button text: "Continue without microphone and camera"
export const zoomPermissionDismissSelector = 'button:has-text("Continue without microphone and camera")';

// ---- In-meeting admission indicators ----

// Leave button: most reliable signal that bot is inside the meeting
// <button aria-label="Leave" class="footer-button-base__button ax-outline footer-button__button">
export const zoomLeaveButtonSelector = 'button[aria-label="Leave"]';

// Audio button in footer (shown when in meeting)
// class="footer-button-base__button ax-outline join-audio-container__btn"
export const zoomAudioButtonSelector = 'button.join-audio-container__btn';

// Video button in footer
export const zoomVideoButtonSelector = 'button.send-video-container__btn';

// Participants button: aria-label contains "participants list"
export const zoomParticipantsButtonSelector = 'button[aria-label*="participants list"]';

// Chat button
export const zoomChatButtonSelector = 'button[aria-label*="chat panel"]';

// The meeting app container
export const zoomMeetingAppSelector = '.meeting-app';

// ---- Host-not-started / invalid meeting ----
// When host hasn't started: title="Error - Zoom", text="This meeting link is invalid (3,001)"
export const zoomInvalidMeetingText = 'This meeting link is invalid';
export const zoomInvalidMeetingTitle = 'Error - Zoom';

// ---- Waiting room indicators ----
// Zoom waiting room: specific text strings appear in DOM (no unique CSS class)
export const zoomWaitingRoomTexts = [
  'Please wait, the meeting host will let you in soon.',
  'Please wait',
  'Waiting for the host to start this meeting',
  'Waiting for the host to start the meeting',
  'waiting room',
  'Waiting Room',
  'Host has joined. We\'ve let them know you\'re here',
];

// ---- Removal / end-of-meeting indicators ----
// Modal: <div class="zm-modal-body-title">This meeting has been ended by host</div>
export const zoomMeetingEndedModalSelector = '.zm-modal-body-title';
export const zoomRemovalTexts = [
  'This meeting has been ended by host',
  'removed from the meeting',
  'meeting has ended',
  'Meeting has ended',
  'ended by the host',
  'You have been removed',
  'host ended the meeting',
];

// ---- Chat panel DOM (when open) ----
// Chat input: TipTap ProseMirror contenteditable inside the RTF editor wrapper
// Verified from live DOM: .chat-rtf-box__editor-outer > .chat-rtf-box__editor-wrapper > ._rtfEditor_* > div[contenteditable]
export const zoomChatInputSelector = '.chat-rtf-box__editor-outer [contenteditable="true"], .tiptap.ProseMirror';
// Chat send button: aria-label="send" on the footer send button
export const zoomChatSendButtonSelector = 'button[aria-label="send"], button[class*="chat-rtf-box__send"]';
// Chat message container — verified: .new-chat-message__container wraps each message
export const zoomChatMessageSelector = '.new-chat-message__container';
// Sender name — verified: .chat-item__sender inside message list items
export const zoomChatSenderSelector = '.chat-item__sender';
// Message text content — verified: .new-chat-message__text-box or .chat-rtf-box__display
export const zoomChatTextSelector = '.new-chat-message__text-box, .chat-rtf-box__display';
// Chat notification banner (new messages)
export const zoomChatNotificationSelector = '[class*="notification-message"]';

// ---- Speaker / participant DOM (in-meeting) ----
// Active speaker tile (main large video frame)
export const zoomActiveSpeakerSelector = '.speaker-active-container__video-frame';
// Speaker bar (non-active thumbnails)
export const zoomSpeakerBarSelector = '.speaker-bar-container__video-frame';
// Participant name label — verified from live DOM: name is in .video-avatar__avatar-footer > span
// (NOT .video-avatar__avatar-name — that element doesn't exist in Zoom Web Client)
export const zoomParticipantNameSelector = '.video-avatar__avatar-footer';
// All video avatar containers
export const zoomVideoAvatarSelector = '.video-avatar__avatar';

// ---- View switching (Speaker View) ----
// Zoom Web defaults to Gallery View. The active-speaker DOM signals used by
// startSpeakerPolling's Layout 1/2 (zoomActiveSpeakerSelector /
// zoomSpeakerBarSelector--active) only populate in Speaker View or during
// screen-share — switchToZoomSpeakerView() (prepare.ts) uses the selectors
// below to force it, once, before recording starts.

// View/Layout footer control — tried in order; Zoom has renamed/reclassed
// this control across versions, so no single selector is trusted alone.
export const zoomViewButtonSelectors: string[] = [
  'button[aria-label="View"]',                       // confirmed live 2026-08-24
  'button.full-screen-widget__button',               // confirmed class of the View control
  'button[aria-label*="View" i]',
  'button[class*="view-toolbar" i], button[class*="footer-button"][class*="view" i]',
  'footer button:has-text("View"), .footer-button-base__button:has-text("View")',
];

// The Speaker-View menu option carries this icon (confirmed live 2026-08-24:
// <svg class="SvgSpeakerView">). It is a UNIQUE, unambiguous anchor — unlike the
// bare word "Speaker" (which also appears in Zoom's audio-device menu) — so it is
// safe to match page-wide and click its nearest clickable ancestor.
export const zoomSpeakerViewIconSelector = 'svg.SvgSpeakerView';

// Overflow "More meeting control" button — only tried if none of
// zoomViewButtonSelectors are visible (some Zoom Web versions / narrow
// viewports push the View control into this overflow menu).
export const zoomMoreButtonSelector =
  'button[aria-label="More"], button[aria-label*="More meeting control" i], button[class*="more-button" i]';

// Container the View menu opens into after the View button is clicked. ALL
// Speaker-View-option candidates below MUST be scoped inside this selector —
// Zoom's separate audio-device menu also has a plain "Speaker (device)"
// entry, so a page-wide has-text("Speaker") match would false-positive on it.
export const zoomViewMenuScopeSelector = '[role="menu"], [class*="view-menu" i], .dropdown-menu';

// Speaker-View menu-item candidates, tried in order, always scoped via
// zoomViewMenuScopeSelector (never page-wide).
export const zoomSpeakerViewOptionSelectors: string[] = [
  '[role="menuitemradio"]:has-text("Speaker")',
  'li:has-text("Speaker View")',
  'button:has-text("Speaker View")',
];

// Last-resort exact-text match (case-insensitive), still scoped inside
// zoomViewMenuScopeSelector, used only if none of the selectors above matched.
export const zoomSpeakerViewExactTexts: string[] = ['Speaker View', 'Speaker Mode'];

// ---- Gallery-view speaking indicators (view-independent) ----
// startSpeakerPolling's Layout 3 fallback: CSS hooks Zoom Web uses to mark a
// participant's Gallery-View tile as currently speaking. Multiple candidates
// because Zoom has changed/renamed these classes across versions and none is
// independently confirmed live — validate against ZOOM_OBSERVE output.
export const zoomGallerySpeakingSelectors: string[] = [
  '.video-avatar__avatar--active',
  '.video-avatar__avatar--speaking',
  '.video-avatar__avatar--talking',
  '.video-avatar__avatar--audio-active',
  '.video-avatar__avatar-border--active',
  '[class*="speaker-active"]',
  '[class*="active-speaker"]',
  '[class*="avatar--active"]',
  '[class*="avatar-active"]',
  '[class*="is-talking"]',
  '[class*="talking"]',
  '[class*="voice-level"]',
  '[class*="audio-animation"]',
  '[class*="speaking-border"]',
  '[class*="speaking-glow"]',
  '[aria-label$="is speaking"]',
  '[aria-label$="is talking"]',
  '[data-speaking="true"]',
  '[data-is-speaking="true"]',
];

// ---- Leave dialog (after clicking Leave button) ----
// Verified from live DOM: the "Leave Meeting" button has class leave-meeting-options__btn--danger
// aria-label is empty so text-based selectors are unreliable; use the CSS class directly
export const zoomLeaveConfirmSelector = 'button.leave-meeting-options__btn--danger';
export const zoomEndForAllSelector = 'button:has-text("End for All")';

// ---- In-meeting mic toggle (mute guarantee) ----
// WHY a list and not `zoomAudioButtonSelector` alone: on 2026-09-01 a live bot
// read aria-label="audio" off `button.join-audio-container__btn` (.first()) for
// the whole meeting while audio WAS flowing — so that first match is not always
// the mute toggle, and its label does not always carry mute vocabulary. Exact
// string equality against 'Mute'/'Unmute' therefore matched nothing and the bot
// sat unmuted in front of other participants. Candidates are tried in order and
// EVERY match of each is probed (not just the first) — the first one that yields
// a confident muted/unmuted reading wins.
//
// Zoom renames CSS classes and changes markup between releases, so NOTHING in
// this list is a contract — every selector here may vanish without notice. That
// is precisely why the mute guarantee does not rest on it: this layer only makes
// the participant list SHOW the bot as muted. The actual guarantee that no audio
// leaves the bot is silenceOutboundAudioTracks() in prepare.ts, which reads no
// DOM at all. If you are here because the bot appeared unmuted, fix these
// selectors; if you are here because the bot was HEARD, the track-level guard is
// what failed and these selectors are not the bug.
//
// Pure CSS only: these are fed to document.querySelectorAll inside page.evaluate,
// so Playwright-only pseudo-classes (:has-text) must NOT appear here. The CSS
// Level 4 case-insensitive attribute flag ([attr*="x" i]) IS supported by Chrome.
// EVERY candidate is scoped to the meeting footer/toolbar, and the live-confirmed
// control comes FIRST. Page-wide mute-vocabulary selectors were removed: Zoom
// renders OTHER buttons whose labels contain mute vocabulary — "Ask to unmute",
// "Unmute All", "Mute All", and per-participant "Mute" rows in the participants
// panel. Because candidates are probed in order and the first confident reading
// wins, a page-wide match could impersonate the mic control. Two distinct
// failures came out of that: "Ask to unmute" reads as MUTED, so the bot reports
// success without ever clicking (a silent failure in the safe-looking
// direction); and "Mute All" reads as UNMUTED, so the watcher would CLICK it and
// mute everyone else in the meeting. zoomNonMicLabelSubstrings below is the
// semantic half of the same defence — scoping alone is not trusted.
//
// One selector per entry (no comma lists): candidates are addressed again as
// page.locator(selector).nth(index) after being probed with querySelectorAll, and
// a comma list makes that index depend on both engines ordering a union
// identically.
export const zoomMicToggleSelectors: string[] = [
  'button.join-audio-container__btn',                                 // confirmed live 2026-09-01 (aria-label="audio")
  'button[class*="join-audio-container" i]',
  'button[class*="footer-button" i][aria-label*="unmute" i]',         // footer-button-base__button, confirmed live
  'button[class*="footer-button" i][aria-label*="mute" i]',
  'footer button[aria-label*="unmute" i]',
  'footer button[aria-label*="mute" i]',
  'footer button[aria-label*="audio" i]',
];

// Labels that belong to a DIFFERENT control which merely CONTAINS mute
// vocabulary. Checked before the mute/unmute branches and returned on
// immediately — never falling through to class hints or aria-pressed, because a
// "Mute All" button may well carry a muted-looking icon class of its own.
// Substring match on the normalised label.
export const zoomNonMicLabelSubstrings: string[] = [
  'mute all',
  'unmute all',
  'ask to unmute',
  'ask all to',
  'mute everyone',
  'unmute everyone',
  'mute participant',
  'unmute participant',
];

// Normalised (lowercased, whitespace-collapsed) aria-labels that identify an
// audio control carrying NO mute/unmute state — either audio is not joined yet
// ("join audio") or Zoom rendered the generic label observed live ("audio").
// Matched as whole-label equality first, then as substrings; 'audio' MUST only
// ever match exactly, because "unmute my audio" contains it.
export const zoomMicNonToggleExactLabels: string[] = ['audio', 'audio settings', 'mic', 'microphone'];
export const zoomMicNonToggleSubstrings: string[] = ['join audio', 'connect audio', 'audio settings'];

// Secondary, UNVERIFIED-LIVE evidence: class hooks Zoom has used to mark the mic
// control's state, read off the button and its icon descendants when neither
// aria-label mute vocabulary nor aria-pressed settled the state. These rank
// BELOW aria-pressed: a real ARIA state attribute beats a guessed class name.
//
// The last entry in each list is the bare word, which is what lets this fallback
// survive Zoom renaming its classes again (the specific spellings above it are
// guesses; 'unmuted'/'muted' will appear in almost any state class Zoom picks).
// It is ALSO what makes the test ORDER load-bearing: 'svgaudiounmuted' contains
// 'muted', so the unmuted list MUST be tested first, or every unmuted control
// reads as muted and the bot never mutes itself. Proven by mutation M3.
export const zoomMicUnmutedClassHints: string[] = ['svgaudiounmuted', 'audio-unmuted', 'is-unmuted', 'mic-unmuted', 'unmuted'];
export const zoomMicMutedClassHints: string[] = ['svgaudiomuted', 'audio-muted', 'is-muted', 'mic-muted', 'muted'];

// ---- Structural discriminators for the mic control (added 2026-09-02) ----
// WHY these exist: on 2026-09-02 the in-meeting control gave NO mute vocabulary
// on aria-label and no aria-pressed, so only the rank-3 class-hint substring
// fired — a PRESENCE TEST over substrings, read as if it were state. Two clicks
// landed on that element (visible, enabled, hit-testable, no interception, no
// Playwright actionability error) and the reading did not move on the next 15s
// poll. So the element is real and clickable and simply was not a mute toggle in
// that state. These give the reader signals that do not depend on Zoom keeping
// its aria vocabulary.

// The label node under a footer button. Zoom renders the state WORD here, which
// is what a human in the room actually reads off the toolbar — and unlike
// aria-label it was NOT missing from the failed run's DOM.
export const zoomMicLabelNodeSelector = '.footer-button-base__button-label, [class*="button-label" i]';

// Text vocabulary that identifies the control as the UNJOINED audio button
// rather than a mute toggle. Substring match on the normalised label/text.
export const zoomMicNotJoinedTextSubstrings: string[] = ['join audio', 'connect audio', 'connect to audio'];

// Split-button caret. Zoom's mic control grows an audio-options chevron beside
// the toggle ONLY once audio is joined, so a caret in the same container is a
// structural marker of the joined state and its absence marks the unjoined one.
//
// UNVERIFIED LIVE — this list is a GUESS. Nothing in readZoomMicState GATES on
// it: it is harvested and reported so one live run can establish whether these
// selectors match the real caret. Promoting it to a discriminator before that
// would risk exactly the failure M30 fixed — a watcher made permanently blind by
// a veto keyed on a selector that never matches.
export const zoomMicCaretSelectors: string[] = [
  '[class*="caret" i]',
  '[class*="chevron" i]',
  '[class*="arrow" i]',
  '[class*="dropdown" i]',
  '[aria-label*="audio option" i]',
  '[aria-label*="audio setting" i]',
  '[aria-label*="mute option" i]',
];

// FULL icon class-name whitelists, matched token-by-token against the class
// attributes of VISIBLY RENDERED descendants. This is the precise tier that
// ranks ABOVE the legacy substring hints above.
//
// WHY a whitelist AND the substrings, rather than replacing them: an exact
// whitelist is precise but goes blind the moment Zoom renames a glyph, and the
// bare-word substring entries in zoomMicUnmutedClassHints exist specifically to
// survive that rename (see the note there, and mutation M3). So the whitelist
// runs FIRST and reports an exact token as its evidence; the substrings stay as
// the lower-confidence fallback, and the evidence string says which tier fired.
// Tokens are compared after normaliseZoomMicText, so list them lowercased.
export const zoomMicIconMutedClasses: string[] = ['svgaudiomuted', 'svgmicmuted', 'svgaudiomutedsmall'];
export const zoomMicIconUnmutedClasses: string[] = ['svgaudiounmuted', 'svgmicunmuted', 'svgaudiounmutedsmall'];
// A headset/join-audio-shaped glyph belongs to the UNJOINED control, which is
// not a mute toggle at all — distinguishing it from a joined mic is the whole
// point of matching full names instead of substrings.
export const zoomMicIconNotJoinedClasses: string[] = ['svgjoinaudio', 'svgheadset', 'svgaudioheadset', 'svgphonecall'];
