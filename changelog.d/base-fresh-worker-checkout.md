Review and judge launches check the worker checkout against origin, including document reviews and reviews with a fresh diff from another checkout.

Fetch distinct reviewed repositories before checking their diff head; only the same resolved worker repository reuses its fetch. Stale tracking refs and failed diff-origin fetches refuse admission for review and judge at both boundaries.
