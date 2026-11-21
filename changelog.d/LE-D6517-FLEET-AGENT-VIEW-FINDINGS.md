Fixed the D-6517 findings of the fleet agent view: the collector and the renderer run the
shared secret redactor over every inventory string, the renderer applies the current
private-word list at publication and hides the inventory when that list is unusable, and the
scheduled `--local` run lists configured remote hosts as unknown instead of dropping them.
