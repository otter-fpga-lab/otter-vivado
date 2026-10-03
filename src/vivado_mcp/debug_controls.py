"""消费者声明的工程控件：精确换算、快照解释和单次写入约束。"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from fractions import Fraction

_BASE = {'id', 'label', 'core', 'uuid', 'probe', 'width', 'direction', 'kind', 'unit'}
_NUMBER = {'signed', 'fractional_bits', 'scale', 'offset', 'min', 'max', 'step'}


def _fields(value, required, optional=()):
    if not isinstance(value, dict) or not required <= value.keys() or (
        value.keys() - required - set(optional)
    ):
        raise ValueError(f'字段必须符合约定：{sorted(required)}')


def _text(value, *, empty=False):
    if not isinstance(value, str) or not int(not empty) <= len(value) <= 1024 or '\0' in value:
        raise ValueError('文本长度必须在允许范围内且不能含 NUL')


def _decimal(value):
    if not isinstance(value, str) or len(value) > 512 or not re.fullmatch(
        r'[+-]?[0-9]+(?:\.[0-9]+)?', value
    ):
        raise ValueError('工程数值必须是普通十进制字符串，不接受浮点数或指数')
    return Fraction(value)


def _format(value):
    """分母仅含 2/5 的有理数转有限小数，不经过浮点数或 Decimal 精度上下文。"""
    n, d = value.numerator, value.denominator
    sign = '-' if n < 0 else ''
    n = abs(n)
    integer, rest = divmod(n, d)
    digits = []
    while rest:
        digit, rest = divmod(rest * 10, d)
        digits.append(str(digit))
    return sign + str(integer) + ('.' + ''.join(digits) if digits else '')


def _hex(raw, width):
    if not isinstance(raw, str) or len(raw) > 66 or not re.fullmatch(
        r'(?:0[xX])?[0-9a-fA-F]+', raw
    ):
        raise ValueError('读回/枚举原始值必须是十六进制文本')
    result = int(raw, 16)
    if result >= 1 << width:
        raise ValueError('原始值超过声明位宽')
    return result


def _wire(number, width):
    return f'0x{number:0{(width + 3) // 4}X}'


def _integer(control, value):
    raw = (value - _decimal(control['offset'])) / _decimal(control['scale'])
    raw *= 1 << control['fractional_bits']
    low = -(1 << (control['width'] - 1)) if control['signed'] else 0
    high = (1 << (control['width'] - int(control['signed']))) - 1
    if raw.denominator != 1 or not low <= raw <= high:
        raise ValueError('工程值不能精确表示或超过位宽；不舍入、不截断')
    return int(raw)


def encode(control, value):
    """数值使用十进制文本，枚举使用 option id；输入探针不可写。"""
    if control['direction'] != 'out':
        raise ValueError('输入探针只读')
    _text(value)
    if control['kind'] == 'enum':
        matches = [o for o in control['options'] if o['id'] == value]
        if len(matches) != 1:
            raise ValueError('未知枚举选项')
        raw = _hex(matches[0]['raw'], control['width'])
    else:
        number = _decimal(value)
        minimum, maximum = _decimal(control['min']), _decimal(control['max'])
        if not minimum <= number <= maximum or (
            (number - minimum) / _decimal(control['step'])
        ).denominator != 1:
            raise ValueError('工程值超出 min/max 或不在从 min 起算的 step 网格上')
        raw = _integer(control, number) % (1 << control['width'])
    return _wire(raw, control['width'])


def decode(control, raw):
    """仅解释实际读回；未知、未映射和超策略值保留原始证据。"""
    bits = raw[2:] if isinstance(raw, str) and raw[:2].lower() == '0x' else raw
    if raw is None or (isinstance(bits, str) and len(bits) <= 64
                       and re.fullmatch(r'[0-9a-fA-FxXzZ?]+', bits)
                       and re.search(r'[xXzZ?]', bits)):
        return {'status': 'unknown', 'raw': raw, 'value': None}
    try:
        number = _hex(raw, control['width'])
    except ValueError as exc:
        return {'status': 'invalid', 'raw': raw, 'value': None, 'error': str(exc)}
    result = {'raw': raw, 'wire_hex': _wire(number, control['width'])}
    if control['kind'] == 'enum':
        found = [o for o in control['options'] if _hex(o['raw'], control['width']) == number]
        return {**result, 'status': 'known' if found else 'unmapped',
                'value': found[0]['id'] if found else None}
    if control['signed'] and number & (1 << (control['width'] - 1)):
        number -= 1 << control['width']
    value = (Fraction(number, 1 << control['fractional_bits']) * _decimal(control['scale'])
             + _decimal(control['offset']))
    minimum, maximum = _decimal(control['min']), _decimal(control['max'])
    in_policy = minimum <= value <= maximum and (
        (value - minimum) / _decimal(control['step'])
    ).denominator == 1
    return {**result, 'status': 'known' if in_policy else 'outside_policy',
            'value': _format(value), 'unit': control['unit']}


def validate_profile(spec):
    """版本 1 描述有界、字段严格；所有语义由工程显式声明。"""
    _fields(spec, {'version', 'title', 'target', 'device', 'controls'}, {'presets'})
    try:
        canonical = json.dumps(spec, sort_keys=True, ensure_ascii=False, allow_nan=False,
                               separators=(',', ':')).encode('utf-8')
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError('描述必须是有效 JSON') from exc
    if len(canonical) > 65536:
        raise ValueError('控件描述不能超过 64 KiB')
    if type(spec['version']) is not int or spec['version'] != 1:
        raise ValueError('仅支持 version=1')
    for key in ('title', 'target', 'device'):
        _text(spec[key])
    if not isinstance(spec['controls'], list) or not 1 <= len(spec['controls']) <= 64:
        raise ValueError('controls 必须含 1~64 项')
    ids, bindings = set(), set()
    for c in spec['controls']:
        if not isinstance(c, dict) or c.get('kind') not in ('number', 'enum'):
            raise ValueError('kind 必须是 number/enum')
        _fields(c, _BASE | (_NUMBER if c['kind'] == 'number' else {'options'}))
        for key in ('id', 'label', 'core', 'uuid', 'probe'):
            _text(c[key])
        _text(c['unit'], empty=True)
        if type(c['width']) is not int or not 1 <= c['width'] <= 256:
            raise ValueError('width 必须是 1~256 的整数')
        if c['direction'] not in ('in', 'out'):
            raise ValueError('direction 必须是 in/out')
        binding = (c['core'], c['probe'])
        if c['id'] in ids or binding in bindings:
            raise ValueError('控件 id 和探针绑定不能重复')
        ids.add(c['id'])
        bindings.add(binding)
        if c['kind'] == 'number':
            if type(c['signed']) is not bool or type(c['fractional_bits']) is not int or not (
                0 <= c['fractional_bits'] <= c['width']
            ):
                raise ValueError('signed 必须为布尔值，fractional_bits 必须在 0~width')
            lo, hi, step, scale = (_decimal(c[k]) for k in ('min', 'max', 'step', 'scale'))
            _decimal(c['offset'])
            if lo > hi or step <= 0 or scale == 0 or ((hi - lo) / step).denominator != 1:
                raise ValueError('范围、步长或 scale 无效；max 必须在步长网格上')
            _integer(c, lo)
            _integer(c, hi)
            if (step / scale * (1 << c['fractional_bits'])).denominator != 1:
                raise ValueError('步长不能精确表示为硬件整数增量')
        else:
            options = c['options']
            if not isinstance(options, list) or not 1 <= len(options) <= 256:
                raise ValueError('options 必须含 1~256 项')
            option_ids, raws = set(), set()
            for option in options:
                _fields(option, {'id', 'label', 'raw'})
                _text(option['id'])
                _text(option['label'])
                if not isinstance(option['raw'], str) or not option['raw'].startswith('0x'):
                    raise ValueError('枚举 raw 必须有 0x 前缀')
                raw = _hex(option['raw'], c['width'])
                if option['id'] in option_ids or raw in raws:
                    raise ValueError('枚举 id 和 raw 不能重复')
                option_ids.add(option['id'])
                raws.add(raw)
    presets = spec.get('presets', [])
    if not isinstance(presets, list) or len(presets) > 32:
        raise ValueError('presets 最多 32 项')
    preset_ids = set()
    for preset in presets:
        _fields(preset, {'id', 'label', 'writes'})
        _text(preset['id'])
        _text(preset['label'])
        if preset['id'] in preset_ids:
            raise ValueError('预设 id 不能重复')
        preset_ids.add(preset['id'])
        _plans(spec, preset['writes'])
    return copy.deepcopy(spec), hashlib.sha256(canonical).hexdigest()


def _control(spec, control_id):
    _text(control_id)
    matches = [c for c in spec['controls'] if c['id'] == control_id]
    if len(matches) != 1:
        raise ValueError('未知控件 id')
    return matches[0]


def _plans(spec, writes):
    if not isinstance(writes, list) or not 1 <= len(writes) <= 64:
        raise ValueError('writes 必须含 1~64 项')
    result, seen = [], set()
    for write in writes:
        _fields(write, {'control_id', 'value'})
        c = _control(spec, write['control_id'])
        if c['id'] in seen:
            raise ValueError('一次预览不能重复写同一控件；不隐式生成脉冲')
        seen.add(c['id'])
        result.append({**write, 'wire_hex': encode(c, write['value']),
                       'core': c['core'], 'probe': c['probe']})
    return result


def bound_probe(spec, control, hardware):
    """逐个精确匹配目标、核 UUID、探针方向与位宽，缺证据则拒绝。"""
    if not isinstance(hardware, dict) or any(
        hardware.get(k) != spec[k] for k in ('target', 'device')
    ):
        raise ValueError('控件 target/device 与硬件快照不匹配')
    cores = [c for c in hardware.get('vios', []) if c.get('name') == control['core']]
    if len(cores) != 1 or cores[0].get('uuid') != control['uuid']:
        raise ValueError('控件 VIO 核名称/UUID 不匹配')
    probes = [p for p in cores[0].get('probes', []) if p.get('name') == control['probe']]
    if len(probes) != 1 or type(probes[0].get('width')) is not int or any(
        probes[0].get(k) != control[k] for k in ('width', 'direction')
    ):
        raise ValueError('控件探针名称/位宽/方向不匹配')
    return probes[0]


def resolve_controls(spec, writes=None, preset=None, hardware=None):
    """离线预览不查询硬件、不执行预设；传入快照仅作为调用方提供的证据。"""
    spec, fingerprint = validate_profile(spec)
    if writes is not None and preset is not None:
        raise ValueError('writes 和 preset 只能提供一个')
    if preset is not None:
        _text(preset)
        found = [p for p in spec.get('presets', []) if p['id'] == preset]
        if len(found) != 1:
            raise ValueError('未知预设')
        writes = found[0]['writes']
    plans = [] if writes is None else _plans(spec, writes)
    controls = []
    for c in spec['controls']:
        item = {'id': c['id'], 'binding': 'unverified', 'readback': None}
        if hardware is not None:
            try:
                p = bound_probe(spec, c, hardware)
                item.update(binding='matched_supplied_snapshot', readback=decode(c, p.get('value')))
            except (ValueError, TypeError, AttributeError) as exc:
                item.update(binding='blocked', error=str(exc))
        controls.append(item)
    return {'profile_sha256': fingerprint, 'profile': spec, 'controls': controls,
            'writes': plans, 'preset': preset, 'executed': False, 'atomic': False,
            'hardware_verified_live': False, 'functional_effect_verified': False}


class ControlWrite:
    """固定一次写入的声明与值；执行前复核实时结构，执行后保留语义回执。"""

    def __init__(self, spec, control_id, value, expected_profile_sha256):
        self.spec, self.fingerprint = validate_profile(spec)
        if expected_profile_sha256 != self.fingerprint:
            raise ValueError('控件描述指纹已变化，请重新预览')
        self.control = _control(self.spec, control_id)
        self.value = value
        self.wire = encode(self.control, value)
        self.params = {'core': self.control['core'], 'probe': self.control['probe'],
                       'value': self.wire}

    def validate(self, hardware):
        bound_probe(self.spec, self.control, hardware)

    def receipt(self, result):
        decoded = decode(self.control, result.get('value'))
        matches = (
            result.get('committed') is True and result.get('readback_matches') is True
            and result.get('core') == self.control['core']
            and result.get('probe') == self.control['probe']
            and type(result.get('width')) is int and result['width'] == self.control['width']
            and decoded.get('wire_hex') == self.wire
        )
        return {'profile_sha256': self.fingerprint, 'control_id': self.control['id'],
                'requested_value': self.value, 'wire_hex': self.wire, 'readback': decoded,
                'readback_verified': matches, 'functional_effect_verified': False}
