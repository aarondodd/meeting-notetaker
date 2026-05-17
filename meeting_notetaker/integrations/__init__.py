"""External integrations (Outlook, etc.).

Each integration submodule lazy-imports its native deps inside functions
so the package can be imported on any platform without crashing.
"""
