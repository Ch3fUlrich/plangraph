"""Writer ordering regression entry points, split from the routing size budget."""


def test_writer_order_sol_family_excluded():
    from test_route_free_first import test_writer_order_sol_family_excluded as check
    check()


def test_writer_order_privacy_excludes_training_sol():
    from test_route_free_first import test_writer_order_privacy_excludes_training_sol as check
    check()
