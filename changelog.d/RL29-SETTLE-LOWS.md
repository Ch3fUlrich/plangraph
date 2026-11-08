RL29: enforce the gitconfig guard even on rejected finish ids; rejection otherwise only sets attempts_error and preserves attempts.
Recovery now reaches finish by booked id; unobserved exit_code stays null, with any old code retained as prior_exit_code. Invalid attribution skips evidence, never settlement.
Normal launch now books an attempt before starting (an intentional record-shape change); the booked array index, including later attempts, is carried into finish.
Fix round 1: assert finish preserves prior_exit_code and outcome_skip_reason; enforce the guard once per settle, retaining selected-attempt diagnostics. Guard rejection uses id 2; the review-artifact fixture already matches base.
