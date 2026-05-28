"""Screen-capture subpackage.

Per-session, manual-only screenshotting of a user-drawn region.
The SessionView's 'Start Screen Capture' button is enabled whenever
a session is selected (the prior RECORDING / PAUSED gate was
relaxed 2026-05-28 to make the capture flow testable outside an
active meeting). Toggling it on launches RegionPicker for the user
to drag a rectangle. While armed, the My Notes sidebar exposes
Capture and Insert buttons that snapshot the region via mss and
save PNG files into session_screenshots_dir(session_id).
"""
