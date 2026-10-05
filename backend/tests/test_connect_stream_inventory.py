"""Reconnect inventory has no request-line ceiling; older GET stays supported."""
import asyncio
import json
import time

import pytest
from starlette.requests import Request
from app import connect_runner
from app.routes import connect

@pytest.mark.asyncio
async def test_post_stream_reconciles_large_inventory_from_body_without_url_ids(client, auth, monkeypatch):
  pairing=client.post('/api/connect/hosts',headers=auth,json={'name':'Fixture'}).json()
  token=client.post('/api/connect/pair',json={'code':pairing['pairing_code']}).json()['token']
  received=[]
  def reconcile(host_id, inventory):
    received.append(inventory)
  monkeypatch.setattr(connect,'_reconcile_runner',reconcile)
  scope={'type':'http','method':'POST','path':'/api/connect/stream','query_string':f'protocol={connect_runner.RUNNER_PROTOCOL_VERSION}&release=5'.encode(),'headers':[(b'authorization',f'Bearer {token}'.encode())]}
  ids=[f'{n:016x}' for n in range(3000)]
  response=await connect.stream(Request(scope),connect.StreamInventory(active_request_ids=ids,pending_result_ids=['f'*16]))
  assert received==[{'active_request_ids':ids,'pending_result_ids':['f'*16]}]
  assert len(scope['query_string'])<100
  assert response.media_type=='text/event-stream'
  connect._channels.pop(pairing['id'],None)

@pytest.mark.asyncio
async def test_legacy_get_stream_still_reconciles_published_query_inventory(client, auth, monkeypatch):
  pairing=client.post('/api/connect/hosts',headers=auth,json={'name':'Fixture'}).json()
  token=client.post('/api/connect/pair',json={'code':pairing['pairing_code']}).json()['token']
  received=[]
  def reconcile(host_id, inventory):received.append(inventory)
  monkeypatch.setattr(connect,'_reconcile_runner',reconcile)
  query=f'protocol={connect_runner.RUNNER_PROTOCOL_VERSION}&active_request_id={"a"*16}&pending_result_id={"b"*16}'
  await connect.stream(Request({'type':'http','method':'GET','path':'/api/connect/stream','query_string':query.encode(),'headers':[(b'authorization',f'Bearer {token}'.encode())]}))
  assert received==[{'active_request_ids':['a'*16],'pending_result_ids':['b'*16]}]
  connect._channels.pop(pairing['id'],None)

@pytest.mark.asyncio
async def test_large_inline_on_old_runner_is_explicitly_update_required(client, auth):
  pairing=client.post('/api/connect/hosts',headers=auth,json={'name':'Fixture'}).json()
  client.post('/api/connect/pair',json={'code':pairing['pairing_code']})
  host_id=pairing['id']; host=connect._load_host(host_id)
  host['runner_capabilities']=['parallel']; connect._save_host(host)
  channel=connect._Channel(); connect._channels[host_id]=channel
  result=await connect.exec_on_host(host_id,connect.ExecBody(cmd='echo ok\n#'+'é'*100_000,request_id='c'*16,stream=True),_owner=object())
  assert result.status_code==409 and json.loads(result.body)['code']=='runner_update_required'
  assert channel.queue.empty() and not connect._host_commands(host_id)
  connect._channels.pop(host_id,None)
