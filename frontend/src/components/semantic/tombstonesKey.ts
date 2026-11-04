// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

/** The query key of the deleted-items list; the page invalidates it after a save (a save
 * that removes an item tombstones it, one that re-adds an item lifts it). */
export const tombstonesKey = (connectionId: string) => ['semantic-tombstones', connectionId];
