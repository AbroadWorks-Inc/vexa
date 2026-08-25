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
