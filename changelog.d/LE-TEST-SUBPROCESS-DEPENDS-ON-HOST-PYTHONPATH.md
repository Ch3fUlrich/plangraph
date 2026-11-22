Rust and Python test subprocess helpers use the checkout's Python source path
and its virtual environment when present. CI identifies test sites that invoke
a bare Python interpreter so package imports work in clean runners.
