Make writer-family launch regressions independent of host credentials by reusing
the existing Go credential fake for the Beam review. Supply an explicit OpenAI
writer in the disk-stop bridge's review requests so they exercise terminal disk
handling after writer-family admission; retain all disk-stop assertions for both
dispatchers and both roles. Production credential handling is unchanged.
