import { describe, expect, it } from 'vitest';
import {
  decodeFocusProjectionEvent,
  decodeFocusRuntimeNoticeDetail,
} from '../src/focus/projectionEventDecoder';
import {
  FOCUS_WEB_RUNTIME_NOTICE_FIELD_LIMIT_BYTES,
} from '../src/focus/focusWire.generated';

describe('Focus runtime notice wire admission', () => {
  it('admits exact typed error and warning fields without rewriting their text', () => {
    expect(decodeFocusRuntimeNoticeDetail({
      method: 'error',
      message: 'retry verbatim',
      additional_details: 'line 1\nline 2',
      will_retry: true,
      turn_id: 'turn-1',
    })).toEqual({
      method: 'error',
      message: 'retry verbatim',
      additional_details: 'line 1\nline 2',
      will_retry: true,
      turn_id: 'turn-1',
    });
    expect(decodeFocusRuntimeNoticeDetail({
      method: 'warning',
      message: 'warning verbatim',
    })).toEqual({ method: 'warning', message: 'warning verbatim' });
  });

  it('rejects unknown fields and malformed discriminated variants', () => {
    expect(decodeFocusRuntimeNoticeDetail({
      method: 'warning', message: 'warning', extra: true,
    })).toBeNull();
    expect(decodeFocusRuntimeNoticeDetail({
      method: 'error',
      message: 'error',
      additional_details: '',
      will_retry: 'true',
      turn_id: 'turn-1',
    })).toBeNull();
  });

  it('enforces the 16 KiB UTF-8 bound and rejects malformed surrogate text', () => {
    expect(decodeFocusRuntimeNoticeDetail({
      method: 'warning',
      message: 'a'.repeat(FOCUS_WEB_RUNTIME_NOTICE_FIELD_LIMIT_BYTES),
    })).not.toBeNull();
    expect(decodeFocusRuntimeNoticeDetail({
      method: 'warning',
      message: 'a'.repeat(FOCUS_WEB_RUNTIME_NOTICE_FIELD_LIMIT_BYTES + 1),
    })).toBeNull();
    expect(decodeFocusRuntimeNoticeDetail({
      method: 'warning',
      message: '界'.repeat(6_000),
    })).toBeNull();
    expect(decodeFocusRuntimeNoticeDetail({
      method: 'warning',
      message: '\uD800',
    })).toBeNull();
  });

  it('admits optional global scope and rejects a malformed runtime notice envelope', () => {
    expect(decodeFocusProjectionEvent({
      type: 'runtime_notice',
      runtime_epoch: 'epoch-1',
      revision: 1,
      detail: { method: 'warning', message: 'global warning' },
    })).toEqual({
      type: 'runtime_notice',
      runtime_epoch: 'epoch-1',
      revision: 1,
      detail: { method: 'warning', message: 'global warning' },
    });
    expect(decodeFocusProjectionEvent({
      type: 'runtime_notice',
      runtime_epoch: 'epoch-1',
      revision: 2,
      detail: { method: 'warning' },
    })).toBeNull();
    expect(decodeFocusProjectionEvent({
      type: 'runtime_notice',
      runtime_epoch: 'epoch-1',
      revision: 3,
      detail: {
        method: 'error',
        message: 'target required',
        additional_details: '',
        will_retry: false,
        turn_id: 'turn-1',
      },
    })).toBeNull();
    expect(decodeFocusProjectionEvent({
      type: 'runtime_notice',
      runtime_epoch: 'epoch-1',
      revision: 4,
      thread_id: '',
      detail: { method: 'warning', message: 'canonical global warning' },
    })).toEqual({
      type: 'runtime_notice',
      runtime_epoch: 'epoch-1',
      revision: 4,
      thread_id: '',
      detail: { method: 'warning', message: 'canonical global warning' },
    });
    expect(decodeFocusProjectionEvent({
      type: 'runtime_notice',
      runtime_epoch: 'epoch-1',
      revision: 5,
      thread_id: 't'.repeat(FOCUS_WEB_RUNTIME_NOTICE_FIELD_LIMIT_BYTES + 1),
      detail: { method: 'warning', message: 'oversized scope' },
    })).toBeNull();
  });
});
