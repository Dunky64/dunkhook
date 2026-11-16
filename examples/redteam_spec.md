# Spec: `parse_duration(text: str) -> int`

Parse a human duration into whole seconds.

- Units: `d` (86400), `h` (3600), `m` (60), `s` (1). Each unit may appear at most once, in that order.
- Components may be separated by optional single spaces: `"1h 30m"`, `"1h30m"`.
- A bare non-negative integer means seconds: `"90"` -> 90.
- Surrounding whitespace is ignored. Matching is case-insensitive.
- Return the total as an `int`.
- Raise `ValueError` for: empty/whitespace-only input, unknown units, repeated or out-of-order
  units, negative numbers, decimals, or any other text.
