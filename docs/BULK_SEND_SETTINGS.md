# BulkSend settings

Durable BulkSend jobs skip missing/invalid usernames, deleted accounts and targets
that resolve to groups or channels, then continue automatically to the next
recipient. Telegram recipient privacy/blocking and definitive destination write
rejections are also skipped. Each result remains in recipient history and the
Skipped total; rejected delivery attempts retain the configured delay.

Missing/expired consent still pauses the job. Resume does…78164 tokens truncated…wner', request, request.usernames, ['a'])
    destination = types.Channel(id=444, title='Synthetic', photo=types.ChatPhotoEmpty(), date=None)
    async def rpc(call):
        if isinstance(call, jobs.functions.channels.GetParticipantRequest):
            data = env.evaluator._load()
            data['consents'] = [row for row in data['consents'] if row['subject_hash'] != policy.hash_identifier('alice_test')]
            env.evaluator.path.write_text(json.dumps(data))
            raise errors.UserNotParticipantError(None)
        pytest.fail('Revoked source consent must prevent invitation')
    client = AsyncMock(side_effect=rpc)
    client.get_me.return_value = types.User(id=999)
    client.get_entity.side_effect = lambda value: destination if value == 'group' else types.User(id=111)
    monkeypatch.setattr(jobs.account_manager, 'get_client', AsyncMock(return_value=client))
    await env.worker.tick()
    assert env.store.snapshot(job, 'owner')['state'] == 'paused'
