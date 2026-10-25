import os
import tempfile
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ['SECRET_KEY'] = 'local-test-secret-not-for-deployment'
os.environ['TUKZIE_DB_PATH'] = str(Path(tempfile.mkdtemp()) / 'users.db')
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
from fastapi.testclient import TestClient
from command_protocol import sign_envelope
from gateway.command_handler import CommandHandler
import pytest

client = TestClient(main.app)


def auth(role='developer'):
    return {'Authorization': 'Bearer ' + main.create_access_token({'sub':role,'role':role})}


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    main._commands.clear()
    main._latest_acks.clear()
    main.latest_telemetry.clear()
    main.field_received_at.clear()
    main.telemetry_times.clear()
    monkeypatch.setattr(main,'last_telemetry_at',None)
    monkeypatch.setattr(main,'INFLUX_TOKEN',None)
    monkeypatch.setattr(main,'mqtt_connected',True)
    broker=MagicMock()
    broker.publish.return_value=SimpleNamespace(rc=0)
    monkeypatch.setattr(main,'mqtt_client',broker)
    main._influx_health_cache.clear()


def submit(cmd='hoot', **extra):
    return client.post('/api/v1/control/command',headers=auth(),json={'cmd':cmd,**extra})


def test_authorization():
    assert client.post('/api/v1/control/command',json={'cmd':'hoot'}).status_code==401
    assert client.post('/api/v1/control/command',headers=auth('guest'),json={'cmd':'hoot'}).status_code==403
    assert client.get('/api/v1/system/health',headers=auth('operator')).status_code==403
    assert submit('lock').status_code==422


def test_command_envelope_and_publish():
    result=submit('message',message='Developer James logged in')
    assert result.status_code==200
    packet=json.loads(main.mqtt_client.publish.call_args.args[1])
    assert packet['message']=='Developer James logged in'
    assert packet['auth_token_v2']==sign_envelope(packet,main.COMMAND_SHARED_SECRET)
    assert packet['command_id']==result.json()['command_id']
    assert main.mqtt_client.publish.call_args.kwargs=={'qos':1,'retain':False}


def test_disconnected_and_publish_failure(monkeypatch):
    monkeypatch.setattr(main,'mqtt_connected',False)
    assert submit().status_code==503
    monkeypatch.setattr(main,'mqtt_connected',True)
    main.mqtt_client.publish.return_value=SimpleNamespace(rc=4)
    assert submit().status_code==503
    assert not main._commands


@pytest.mark.parametrize('body',[{'cmd':'message','message':''},{'cmd':'message','message':'x'*281},{'cmd':'invalid'},{'cmd':[]},{'cmd':'hoot','target':'someone'}])
def test_bad_commands(body):
    assert client.post('/api/v1/control/command',headers=auth(),json=body).status_code==422


def test_correlated_ack_never_reuses_old_ack():
    first=submit().json()['command_id']
    main.record_ack({'command_id':first,'cmd':'hoot','status':'ack'})
    second=submit().json()['command_id']
    main.record_ack({'command_id':first,'cmd':'hoot','status':'ack'})
    main.record_ack({'cmd':'hoot','status':'ack'})
    pending=client.get(f'/api/v1/control/status/hoot?command_id={second}',headers=auth()).json()
    assert pending['status']=='pending'
    main.record_ack({'command_id':second,'cmd':'hoot','status':'rejected','note':'Hardware unavailable'})
    result=client.get(f'/api/v1/control/status/hoot?command_id={second}',headers=auth()).json()
    assert result['status']=='rejected'
    assert result['latency_ms']>=0
    assert client.get(f'/api/v1/control/status/hoot?command_id={second}',headers=auth('operator')).status_code==404


def test_timeout():
    command_id=submit().json()['command_id']
    main._commands[command_id]['timestamp']-=11
    main.record_ack({'command_id':command_id,'cmd':'hoot','status':'ack'})
    assert client.get(f'/api/v1/control/status/hoot?command_id={command_id}',headers=auth()).json()['status']=='timeout'


def test_empty_live_feed_and_telemetry_metadata():
    assert client.get('/api/v1/telemetry/live').json()=={}
    main.on_mqtt_message(None,None,SimpleNamespace(topic=main.MQTT_TOPIC,payload=b'{"bSOC":72,"cImu":{"pitch":2,"roll":3}}',retain=False))
    packet=client.get('/api/v1/telemetry/live').json()
    assert packet['bSOC']==72
    assert packet['_field_received_at']['bSOC']>0
    assert packet['_server_time']>=packet['_received_at']
    main.on_mqtt_message(None,None,SimpleNamespace(topic=main.MQTT_TOPIC,payload=b'{"bSOC":99}',retain=True))
    assert main.latest_telemetry['bSOC']==72


def test_health_states(monkeypatch):
    monkeypatch.setattr(main,'mqtt_connected',False)
    health=client.get('/api/v1/system/health',headers=auth()).json()
    assert health['web_to_broker']=='disconnected'
    assert health['gateway_to_broker']=='disconnected'
    assert health['database']['status']=='not_configured'
    for age,expected in [(1,'healthy'),(9,'warning'),(20,'disconnected')]:
        monkeypatch.setattr(main,'last_telemetry_at',time.time()-age)
        assert client.get('/api/v1/system/health',headers=auth()).json()['gateway_to_broker']==expected


def test_history_auth_and_validation(monkeypatch):
    assert client.get('/api/v1/telemetry/history').status_code==401
    assert client.get('/api/v1/telemetry/history',headers=auth()).status_code==503
    monkeypatch.setattr(main,'INFLUX_TOKEN','test')
    for query in ['range=bad','field=a%22)%20%7C%3E%20drop()','format=exe']:
        assert client.get('/api/v1/telemetry/history?'+query,headers=auth()).status_code==422


def test_history_sort_csv_and_limit(monkeypatch):
    monkeypatch.setattr(main,'INFLUX_TOKEN','test')
    monkeypatch.setattr(main,'HISTORY_LIMIT',2)
    records=[]
    for stamp,value in [(3,2),(1,1),(2,'a,b\n"c"')]:
        record=MagicMock()
        record.get_time.return_value=datetime.fromtimestamp(stamp,timezone.utc)
        record.get_value.return_value=value
        record.get_field.return_value='bSOC'
        records.append(record)
    fake=MagicMock()
    fake.return_value.__enter__.return_value.query_api.return_value.query.return_value=[SimpleNamespace(records=records)]
    monkeypatch.setattr(main,'InfluxDBClient',fake)
    result=client.get('/api/v1/telemetry/history',headers=auth()).json()
    assert result['truncated'] is True and len(result['points'])==2
    assert result['points'][1]['value']==2
    csv=client.get('/api/v1/telemetry/history?format=csv',headers=auth())
    assert csv.status_code==200 and '"a,b\n""c"""' in csv.text


def test_public_config_does_not_expose_internal_url_or_token(monkeypatch):
    monkeypatch.setattr(main,'INFLUX_UI_URL','https://db.example.test')
    assert client.get('/api/v1/config',headers=auth()).json()['influx_ui_url']=='https://db.example.test'
    monkeypatch.setattr(main,'INFLUX_UI_URL','javascript:alert(1)')
    assert client.get('/api/v1/config',headers=auth()).json()['influx_ui_url'] is None


def test_gateway_validation_deduplication_and_failures():
    execute=MagicMock(return_value='Applied')
    handler=CommandHandler('test-secret',execute)
    packet={'cmd':'message','target':'vehicle','timestamp':time.time(),'command_id':'test-id','message':'hello'}
    packet['auth_token_v2']=sign_envelope(packet,'test-secret')
    ack=handler.handle(packet)
    assert ack['status']=='ack'
    assert handler.handle(packet)==ack
    execute.assert_called_once_with('message','hello')
    assert handler.handle({**packet,'message':'tampered'}) is None
    failed={**packet,'command_id':'second'}
    failed['auth_token_v2']=sign_envelope(failed,'test-secret')
    execute.side_effect=RuntimeError('Device not connected')
    assert handler.handle(failed)['status']=='rejected'


def test_real_login_and_invalid_password():
    if not main.database.get_user('integration-login'):
        main.database.create_user('integration-login','test-password-123','developer')
    response=client.post('/token',data={'username':'integration-login','password':'test-password-123'})
    assert response.status_code==200
    token=response.json()['access_token']
    assert client.get('/api/v1/config',headers={'Authorization':'Bearer '+token}).status_code==200
    assert client.post('/token',data={'username':'integration-login','password':'wrong'}).status_code==400


def test_location_is_redacted_for_guests():
    import main
    data = {"bSOC": 80, "lat": -33.9, "lon": 18.4, "gps": {"lat": 1}, "_field_received_at": {"lat": 1.0, "bSOC": 1.0}}
    guest = main.redact_for_role(data, "guest")
    assert "lat" not in guest and "lon" not in guest and "gps" not in guest
    assert guest["bSOC"] == 80 and "lat" not in guest["_field_received_at"]
    assert main.redact_for_role(data, "operator") is data
    assert main.role_from_token("not-a-token") == "guest"
    token = main.create_access_token({"sub": "developer", "role": "developer"})
    assert main.role_from_token(token) == "developer"
