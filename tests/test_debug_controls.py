"""工程语义精确换算与共享服务防护，硬件仅使用明确测试替身。"""

import asyncio
import copy

import pytest

from tests.test_debug_service import action, hardware, select
from tests.test_debug_service import service as service
from vivado_mcp.debug_controls import decode, resolve_controls, validate_profile


def profile():
    return {'version': 1, 'title': '增益', 'target': 'cable/serial', 'device': 'xc7_0',
            'controls': [{'id': 'gain', 'label': '增益', 'core': 'vio', 'uuid': 'vio-uuid',
                          'probe': 'gain', 'width': 8, 'direction': 'out', 'kind': 'number',
                          'unit': 'V', 'signed': True, 'fractional_bits': 4, 'scale': '2',
                          'offset': '1', 'min': '-15', 'max': '16.875', 'step': '0.125'}],
            'presets': [{'id': 'idle', 'label': '空闲',
                         'writes': [{'control_id': 'gain', 'value': '0'}]}]}


def preview(spec=None, value='0', **kwargs):
    return resolve_controls(spec or profile(), [{'control_id': 'gain', 'value': value}], **kwargs)


def enum_profile():
    spec = profile()
    c = spec['controls'][0]
    for key in ('signed', 'fractional_bits', 'scale', 'offset', 'min', 'max', 'step'):
        del c[key]
    c.update(kind='enum', options=[{'id': 'on', 'label': '有效低', 'raw': '0x00'},
                                   {'id': 'off', 'label': '关闭', 'raw': '0xFF'}])
    spec['presets'][0]['writes'][0]['value'] = 'off'
    return spec


@pytest.mark.parametrize('value,wire', [('0', '0xF8'), ('-15', '0x80'),
                                        ('16.875', '0x7F'), ('1', '0x00')])
def test_fixed_point_signed_offset_exact_roundtrip(value, wire):
    result = preview(value=value)
    assert result['writes'][0]['wire_hex'] == wire
    c = result['profile']['controls'][0]
    assert decode(c, wire[2:])['value'] == value
    assert result['executed'] is False
    assert result['hardware_verified_live'] is False


def test_wide_unsigned_never_passes_through_float():
    spec = profile()
    maximum = str((1 << 80) - 1)
    spec['controls'][0].update(width=80, signed=False, fractional_bits=0, scale='1',
                                offset='0', min='0', max=maximum, step='1')
    result = preview(spec, maximum)
    assert result['writes'][0]['wire_hex'] == '0x' + 'F' * 20
    assert decode(spec['controls'][0], 'F' * 20)['value'] == maximum


def test_negative_scale_and_decimal_units():
    spec = profile()
    spec['controls'][0].update(scale='-0.2', offset='0.1', min='-1.4875',
                                max='1.7', step='0.0125')
    assert preview(spec, '1.7')['writes'][0]['wire_hex'] == '0x80'
    assert decode(spec['controls'][0], '80')['value'] == '1.7'


@pytest.mark.parametrize('value', ['0.01', '17', '-15.125', '1e0', 'NaN', 0, True, 0.125])
def test_refuse_rounding_overflow_or_numeric_json(value):
    with pytest.raises(ValueError):
        preview(value=value)


def test_enumerations_active_low_unknown_and_readonly():
    spec = enum_profile()
    assert preview(spec, 'on')['writes'][0]['wire_hex'] == '0x00'
    assert resolve_controls(spec, preset='idle')['writes'][0]['wire_hex'] == '0xFF'
    c = spec['controls'][0]
    assert decode(c, 'FF')['value'] == 'off'
    assert decode(c, '01')['status'] == 'unmapped'
    with pytest.raises(ValueError, match='枚举'):
        preview(spec, '0')
    spec.pop('presets')
    c['direction'] = 'in'
    with pytest.raises(ValueError, match='只读'):
        preview(spec, 'on')
    assert resolve_controls(spec)['controls'][0]['binding'] == 'unverified'


@pytest.mark.parametrize('raw,status', [(None, 'unknown'), ('X', 'unknown'), ('Z', 'unknown'),
                                         ('100', 'invalid'), ('garbage', 'invalid'),
                                         ('', 'invalid'), (0, 'invalid')])
def test_bad_readback_is_never_zero(raw, status):
    result = decode(profile()['controls'][0], raw)
    assert result['status'] == status
    assert result['value'] is None


def test_decode_actual_hex_not_decimal_or_staged_and_outside_policy():
    spec, hw = profile(), hardware()
    hw['vios'][0]['probes'][0].update(value='10', staged_value='80')
    result = preview(spec, hardware=hw)
    assert result['controls'][0]['readback']['value'] == '3'
    spec['controls'][0].update(min='0', max='1', step='0.25')
    assert decode(spec['controls'][0], '10')['status'] == 'outside_policy'
    assert decode(spec['controls'][0], 'F9')['status'] == 'outside_policy'


@pytest.mark.parametrize('change', [
    {'width': True}, {'width': 257}, {'fractional_bits': 9}, {'signed': 'yes'},
    {'scale': '0'}, {'step': '0.01'}, {'step': '-1'}, {'max': '17'},
    {'offset': 'NaN'}, {'unexpected': 0}, {'direction': 'unknown'}, {'uuid': ''},
])
def test_invalid_number_profile(change):
    spec = profile()
    spec['controls'][0].update(change)
    with pytest.raises(ValueError):
        validate_profile(spec)


def test_profile_identity_bounds_duplicates_and_order():
    spec = profile()
    digest = validate_profile(spec)[1]
    assert validate_profile(dict(reversed(list(spec.items()))))[1] == digest
    spec['controls'][0]['unit'] = 'mV'
    assert validate_profile(spec)[1] != digest
    spec['controls'].append(copy.deepcopy(spec['controls'][0]))
    with pytest.raises(ValueError, match='重复'):
        validate_profile(spec)
    spec = profile()
    spec['presets'][0]['writes'] *= 2
    with pytest.raises(ValueError, match='重复'):
        validate_profile(spec)
    spec = enum_profile()
    spec['controls'][0]['options'][1]['raw'] = '0x0'
    with pytest.raises(ValueError, match='重复'):
        validate_profile(spec)
    spec = profile()
    spec['title'] = '长' * 30000
    with pytest.raises(ValueError, match='64 KiB'):
        validate_profile(spec)
    spec = profile()
    second = copy.deepcopy(spec['controls'][0])
    second.update(id='second', probe='second')
    spec['controls'].append(second)
    spec['presets'][0]['writes'].append({'control_id': 'second', 'value': '1'})
    result = resolve_controls(spec, preset='idle')
    assert [w['control_id'] for w in result['writes']] == ['gain', 'second']
    spec['presets'][0]['writes'].reverse()
    assert validate_profile(spec)[1] != result['profile_sha256']
    with pytest.raises(ValueError):
        resolve_controls(spec, [], preset='idle')


@pytest.mark.parametrize('field,value', [('target', 'wrong'), ('device', 'wrong'),
                                         ('uuid', 'other'), ('width', 7), ('direction', 'in')])
def test_snapshot_binding_mismatch(field, value):
    hw = hardware()
    if field in ('target', 'device'):
        hw[field] = value
    elif field == 'uuid':
        hw['vios'][0][field] = value
    else:
        hw['vios'][0]['probes'][0][field] = value
    result = preview(hardware=hw)
    assert result['controls'][0]['binding'] == 'blocked'
    assert result['controls'][0]['readback'] is None


def submit(service, spec=None, value='0', **kwargs):
    spec = spec or profile()
    return service.submit_control(spec, 'gain', value,
                                  kwargs.pop('fingerprint', validate_profile(spec)[1]),
                                  kwargs.pop('revision', service.snapshot()['revision']),
                                  source=kwargs.pop('source', 'manual'))


def receipt():
    return {'core': 'vio', 'probe': 'gain', 'width': 8, 'committed': True,
            'readback_matches': True, 'value': 'F8', 'functional_effect_verified': False}


async def test_control_write_and_receipt(service):
    await select(service)
    service.backend.write_vio.return_value = receipt()
    accepted = submit(service)
    await service.wait_idle()
    op = service.snapshot()['operations'][-1]
    assert op['id'] == accepted['operation_id'] and op['status'] == 'succeeded'
    assert op['result']['semantic']['readback']['value'] == '0'
    assert op['result']['semantic']['readback_verified'] is True
    assert op['result']['semantic']['functional_effect_verified'] is False
    service.backend.write_vio.assert_awaited_once_with(
        'cable/serial', 'xc7_0', core='vio', probe='gain', value='0xF8', expected_uuid='vio-uuid')


async def test_profile_revision_control_and_busy_guards(service):
    await select(service)
    with pytest.raises(ValueError, match='指纹'):
        submit(service, fingerprint='old')
    with pytest.raises(RuntimeError, match='状态已变化'):
        submit(service, revision=0)
    with pytest.raises(RuntimeError, match='控制权'):
        submit(service, source='ai')
    service.backend.write_vio.assert_not_awaited()
    submit(service)
    with pytest.raises(RuntimeError, match='已有调试'):
        submit(service)
    await service.wait_idle()


async def test_fresh_identity_rechecked_before_write(service):
    await select(service)
    submit(service)
    service.fixture_hardware['vios'][0]['uuid'] = 'reprogrammed'
    await service.wait_idle()
    assert service.snapshot()['operations'][-1]['status'] == 'failed'
    service.backend.write_vio.assert_not_awaited()
    # 即使新拓扑已经进入缓存，旧描述依然不可写。
    await action(service, 'refresh')
    with pytest.raises(ValueError, match='UUID'):
        submit(service)


@pytest.mark.parametrize('change', [{'value': 'F9'}, {'value': None}, {'width': 7},
                                     {'probe': 'other'}, {'committed': False},
                                     {'readback_matches': False}])
async def test_mismatched_receipt_is_unknown_preserves_evidence(service, change):
    await select(service)
    service.backend.write_vio.return_value = {**receipt(), **change}
    submit(service)
    await service.wait_idle()
    op = service.snapshot()['operations'][-1]
    assert op['status'] == 'unknown'
    assert op['result']['semantic']['readback_verified'] is False
    for key, value in change.items():
        assert op['result'][key] == value
    with pytest.raises(RuntimeError, match='最新硬件'):
        submit(service)
    assert service.backend.write_vio.await_count == 1


async def test_accepted_spec_is_copied_and_postwrite_failure_keeps_receipt(service):
    await select(service)
    spec = profile()
    service.backend.write_vio.return_value = receipt()
    calls = 0

    async def inspect(*args):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError('lost after write')
        return hardware()

    service.backend.inspect.side_effect = inspect
    submit(service, spec)
    spec['controls'][0]['offset'] = '20'
    await service.wait_idle()
    op = service.snapshot()['operations'][-1]
    assert op['status'] == 'unknown'
    assert op['result']['semantic']['readback_verified'] is True
    assert op['result']['semantic']['readback']['value'] == '0'


async def test_control_writes_respect_experiment_lease(service, tmp_path):
    from vivado_mcp.debug_experiment import DebugExperiment

    await select(service)
    DebugExperiment(service, {'title': 'lease', 'steps': [
        {'id': 'ready', 'kind': 'ready', 'label': '确认'}]},
        str(tmp_path / 'exp'), service.snapshot()['revision'], owner='manual')
    exp = service.experiment
    exp.command('start', {}, exp.revision, source='manual')
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match='活动实验'):
        submit(service)
    service.backend.write_vio.assert_not_awaited()
