# Players

## Respawning through the host
On the `Player.RespawnBro` path the host picks the hero type, so the spawn starts on the player's owning client and round-trips through the host. `RespawnBro` runs on the owning client, called from `HeroController.RunHeroRespawnLogic` or by an RPC from the host → `HeroController.RequestHeroTypeFromMaster` → RPC to `PID.TargetServer` (`Net/`) `RequestHeroTypeFromMasterRPC` (the host runs `GetHeroType`, or uses `GameModeController.deathmatchHero` (`GameFlow/`) in deathmatch and race) → RPC back to the requester `RecieveHeroTypeFromMaster` → `Player.SpawnHero(type)`.
`HeroController.ChangeBro` skips the round trip with a locally chosen type: it destroys the current character (a `BoondockBro` (`Bros/`) group is handled differently) and calls `SpawnHero` itself.

## From SpawnHero to a placed bro
`Player.SpawnHero` recalls a living old bro (`RecallBro` RPC) and does the unlock bookkeeping, then calls `InstantiateHero`, which does nothing on a non-owner:
1. `HeroController.GetHeroPrefab(type)`; a `HeroType` (`Bros/`) missing from `_heroData` silently gives Rambro's prefab.
2. `Networking.InstantiateBuffered` (`Networking/`); buffered, so late joiners receive the bro.
3. RPC to all `SetUpHero(playerNum, type, true)` → `HeroController.RegisterHeroToPlayer`, which sets `player.character` and `hero.player`.
4. `Player.WorkOutSpawnPosition`: `WorkOutSpawnScenario` picks a `SpawnType` (hero transport, spawn point or cage, checkpoint, airdrop, rescued bro, bro swap...). The transport case also sends `Map.AddBroToHeroTransport` (`Level/`) to all, which for a bro class a mod declares runs on no client, so the bro boards no transport.
5. RPC to all `Player.SetSpawnPositon(bro, spawnType, airdrop, pos)` → `SetXY`, `SetPosition`, `spawnPositionHasBeenSet = true`, and clears the player's `firstDeployment`. For a bro class a mod declares it runs on no client: nothing places the bro, which starts at the origin `InstantiateBuffered` gave it, and on a first deployment `HeroController.RunHeroRespawnLogic` respawns the player once the 15 s `serverResponseTimeout` clears `awaitingHeroTypeFromServer`.
