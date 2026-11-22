Fixed the D-6517 findings of the UI auto deploy: the `/plan` nginx template requires authentication, the live
follow gate counts only CI runs bound to the merged PR and its target branch, the render service runs only
from the head the gate recorded as green (`live_follow verified`), and gate state defaults to a private path
and is refused inside the web-served output directory.
