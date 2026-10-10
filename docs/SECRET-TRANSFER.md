# Secret transfer (DECISIONS 5.9 / D-944)

Sender tool: `tools/secret_transfer.py` (age-based). Order is enforced; `send` is refused until a test round-trip is confirmed.

## Receiver

    umask 077; mkdir -p ~/.config/age; age-keygen -o ~/.config/age/<host>.key   # prints the public age1... recipient

Give the sender only the public `age1...` recipient.

## Sender order

1. `secret_transfer.py recipient add NAME AGE1...` (re-adding a different key resets the test)
2. `secret_transfer.py test NAME --out test.age` -> send `test.age` to the receiver.
   Receiver decrypts (`age -d -i ~/.config/age/<host>.key -o /tmp/t test.age`), computes the first 8 hex of its sha256 and reports it independently.
3. `secret_transfer.py confirm NAME PREFIX` (mismatch: exit 2, stays unconfirmed)
4. `secret_transfer.py send NAME --in SECRET --out secret.age` (SECRET: regular file, mode 0600, not a symlink). Prints out path, size, sha256 prefix.
5. `secret_transfer.py shred NAME --in SECRET` after the receiver confirms.

## Receiver decrypt

    umask 077; age -d -i ~/.config/age/<host>.key -o <dest> secret.age; chmod 600 <dest>

Never display the content. Report only path, mode, size and sha256 prefix (8 hex) to match the sender's value.
