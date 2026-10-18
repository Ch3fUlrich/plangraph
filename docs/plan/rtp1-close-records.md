# RT-P1 close records

Merged code predates the keeper claims (the items were gated until D-1964), so each item closes on a
records commit made after its claim. Seat and judge records are kept privately; this lists the facts.

| Item | PR | Head | Merge | Review |
|---|---|---|---|---|
| plangraph:RT-P1-WORKER | #46 | cbf83e48 | 30b738ba | HIGH; seats gemini + mimo; judge NO-GO, fixes C1/C2, GO on cbf83e48 (deploy: one worker host per repo) |
| plangraph:RT-P1-PROJECTOR | #47 | b4726d62 | 76e7bffc | HIGH; seats gemini + mimo + qwen; judge GO (D-1934) |
| plangraph:RT-P1-REVIEW | #48 | d4028534 | b9a5d84e | standard; seats gemini + mimo pass |
| plangraph:RT-P1-INBOX | #49 | 34997f4b | 95fd6d9e | standard; seats gemini + qwen; dismissals checked by build-L1 |
