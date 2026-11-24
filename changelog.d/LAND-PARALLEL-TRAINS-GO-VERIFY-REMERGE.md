The train replay now names `main_in`, the newest main it proved the train contains (the newest
remerged main, else the base); `land-train verify` prints it and the review artifact carries it.
The GO verifier accepts a remerged train whose replay `main_in` equals main's tip and the pull
request base, and still refuses a stale base in every other case.
