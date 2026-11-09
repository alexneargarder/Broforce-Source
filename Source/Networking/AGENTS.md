# Networking

Code outside namespace `Networking`, a mod's own namespace included, calls the hub as `Networking.Networking.RPC(...)`, with `global::` in front where another `Networking` is in scope: the static class and its namespace share a name, and a bare `Networking.RPC` names the namespace unless `using Networking;` sits inside the caller's `namespace` block.

## Sending
`Networking.RPC` / `AdminRPC` / `UnreliableRPC` → `RPCController.SendRPC` (`Net/`) → `CreateRPC` → `RouteRPCObject` → `RPCBatcher.Send` (`Net/`) → `QueueSend`.
- `Connect.BypassNetworkLayer` (`Net/`) is true only in the Unity editor, so the shipped game always takes this path, offline too: arguments are serialized and the call runs through `Execute` even locally.
- `CreateRPC` addresses an instance method by `Registry.GetNID(target)` (`Core/`); an unregistered target gets `NID.NoID` (`Net/`).
- `RouteRPCObject`: `TargetAll` executes on the sender at once and also sends; `TargetOthers` only sends; `TargetServer` executes locally only on the host and only sends from a client. The `useReliableStream` parameter is never read, so `UnreliableRPC` travels like `RPC`.
- `RPCBatcher.QueueSend` drops, without a trace, every non-admin RPC addressed to a peer whose `IDWrapper.HaveSentSyncStateToPlayer` (`Net/`) is false. A new peer's flag stays false until this machine answers its sync request (`Registry.RequestNetworkObjectSync`, which every online `GameState.LoadLevel` (`GameFlow/`) sends). The handshake runs once per peer: a later request returns at once on `haveRequestedSyncStateFromPlayer`, and a level load clears neither flag, because only `Registry.Start` clears them and each scene's copy of the `Connect` object, `Registry` included, is destroyed in `Awake`, so `Start` runs only on the new `Connect` that `MainMenu.Awake` (`Menus/`) creates. `AdminRPC` skips the check; vanilla uses it to replay state during the handshake.

## Networked instantiation
`Networking.Instantiate*` → `InstantiationController.NetworkedInstantiate` (`Core/`).
- The prefab must be in `InstantiationController.PrefabList`, built once from the `spawnables` `NetworkSpawnableManifest`, and the lookup is by reference. Anything else (a prefab built at runtime, a clone) gets index -1: the call returns null with no RPC and no log. Remote clients receive only the index, so a prefab appended to the list resolves only if it sits at the same index on every client.
- On the spawning client: serialize the instantiation data → `Registry.AllocatePlayerUniqueID` → `StoreTempData` on the prefab (so the clone's `Awake` can read `NetworkObject.InstantiationData` (`Net/`)) → `Object.Instantiate` → `ClearTempData` → `InitializeInstance` (`Registry.RegisterGameObject`, `NetworkObject.NetworkSetup`) → `InstantiateRPC` to the others. A clone whose registration fails is destroyed. No step calls `SetActive`, so the clone keeps the prefab's active state.
- Only `InstantiateBuffered` / `InstantiateSceneOwned` objects are replayed to late joiners (`SendInstantiatedPrefabs`, and `SendSceneOwnedObjects` from the host).
