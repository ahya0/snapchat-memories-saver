# Product roadmap

The first product is the local skill. Mobile apps come after the workflow is proven with real users.

## Product boundary

Snapchat Memories Saver helps people preserve data Snapchat includes in an official export. It does not sign in for users, bypass access controls, scrape private endpoints, or claim that it replaces any Snapchat subscription feature. Users control their own export and storage.

## Stage 1: reviewed local skill

- Walk the user through requesting a Memories-only, all-time export.
- Check every extracted part without modifying it.
- Produce a clear complete or recovery page.
- Preserve original ZIPs, extracted exports, and private reports.
- Provide fixed safe error codes and a privacy-reduced support summary that warns about aggregate counts.
- Support a double-click macOS launcher without weakening CLI validation.

Exit gate: personal end-to-end test in a fresh session using the same pinned version.

## Stage 2: unique local library

Build this as a separate command after the personal test. It must:

- Accept only a complete, supported audit report and the same ordered export roots.
- Copy one audited representative of each logical Snapchat media file into a brand-new library.
- Preserve photos, videos, and overlays without changing source exports.
- Check destination free space, copy through private staging, verify every copied hash, and publish atomically.
- Never hardlink source files or publish a partial library.

This is the preferred public deduplication experience: create a verified unique library instead of deleting evidence from raw exports.

## Stage 3: duplicate cleanup

Cleanup remains separate from both audit and library creation. It begins with a dry-run plan and does not act until a later explicit approval.

Required safeguards:

- Never target original ZIPs, reports, or files outside one explicitly managed root.
- Prove duplicates with size, SHA-256, and byte-for-byte comparison.
- Preserve one verified copy before any removal.
- Bind the plan to device, inode, size, modification time, hash, and a canonical plan digest.
- Revalidate everything immediately before acting.
- Move candidates to a reversible quarantine first, keep an operation journal, and provide a tested restore path.
- Require a separate typed confirmation for permanent deletion.

Exit gate: successful personal library build, independent security review, synthetic interruption tests, and explicit approval for the exact cleanup plan.

## Stage 4: friends beta

- Share a pinned beta version with a few Mac users.
- Keep all exports local.
- Collect only fixed error codes, `support.txt`, and the privacy-safe fields in [`BETA.md`](BETA.md).
- Fix confusing steps before a broader release.

## Stage 5: iPhone and Android apps

The mobile app should keep the same trust model:

- The user requests and downloads their own official export.
- The app imports ZIP parts through the operating system's file picker or share sheet.
- Processing happens on-device by default.
- The app never asks for Snapchat credentials, cookies, verification codes, or signed per-Memory URLs.
- Originals remain immutable; any clean library is a new verified destination.
- Recovery guidance is honest when Snapchat omits evidence.
- Cloud backup is optional, clearly disclosed, encrypted, and never required for local checking.

Start with a local import prototype only after the skill and friends beta prove the export formats, recovery UX, and storage requirements.

## Stage 6: public launch

Publish the reviewed release first. Verify a clean installation from its immutable tag, use only synthetic screenshots or demo data, and post on X only after personal testing and the friends beta are complete.
