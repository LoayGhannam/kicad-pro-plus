# patches/

KiCad Pro+ changes to the upstream KiCad source, as git patches applied in order
(`0001-*.patch`, `0002-*.patch`, ...) on top of the tag in `ci/KICAD_VERSION`.

Keeping our changes as patches (instead of committing the 1.4 GB KiCad tree) keeps
this repo small and makes every change easy to review, revert, or re-apply on a newer
KiCad if we ever want to.
