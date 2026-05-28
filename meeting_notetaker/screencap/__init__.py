"""Screen-capture subpackage.

Per-session, manual-only screenshotting of a user-drawn region. The
capture flow is gated on an active recording: the SessionView's
'Start Screen Capture' button is disabled unless state is RECORDING
or PAUSED, and toggling it on launches RegionPicker for the user to
drag a rectangle. While armed, the My Notes sidebar exposes Capture
and Insert buttons that snapshot the region via mss and save PNG
files into session_screenshots_dir(session_id).
"""
