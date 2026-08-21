import { describe, expect, it } from 'vitest';
import {
  IMG_PLACEHOLDER,
  collectLocalImageSources,
  rewriteLocalImageSources,
} from '../src/lib/markdownLocalImages';

describe('Markdown local-image policy', () => {
  it('finds only browser-unaddressable image sources', () => {
    expect(collectLocalImageSources([
      '![relative](assets/chart.png)',
      '<img src="/srv/work/diagram.png">',
      "<img src='relative/single-quoted.png'>",
      '<img alt=diagram src=relative/unquoted.png>',
      '![remote](https://example.test/image.png)',
      '![embedded](data:image/png;base64,AAAA)',
    ].join('\n'))).toEqual([
      'assets/chart.png',
      '/srv/work/diagram.png',
      'relative/single-quoted.png',
      'relative/unquoted.png',
    ]);
  });

  it('renders an explicit refusal as inert text without retaining the server path', () => {
    const text = [
      'Before ![chart](/srv/private/project/chart.png) after.',
      '<img src="relative/diagram.png">',
      "<img src='relative/diagram-single.png'>",
      '<img src=relative/diagram-unquoted.png>',
      '![remote](https://example.test/image.png)',
    ].join('\n');
    const result = rewriteLocalImageSources(text, {
      enabled: true,
      resolvedImages: new Map([
        ['/srv/private/project/chart.png', null],
        ['relative/diagram.png', null],
        ['relative/diagram-single.png', null],
        ['relative/diagram-unquoted.png', null],
      ]),
      unavailableText: 'Local image is unavailable in Focus Web.',
    });

    expect(result).toBe([
      'Before Local image is unavailable in Focus Web. after.',
      'Local image is unavailable in Focus Web.',
      'Local image is unavailable in Focus Web.',
      'Local image is unavailable in Focus Web.',
      '![remote](https://example.test/image.png)',
    ].join('\n'));
    expect(result).not.toContain('/srv/private/project/chart.png');
    expect(result).not.toContain('relative/diagram.png');
    expect(result).not.toContain('relative/diagram-single.png');
    expect(result).not.toContain('relative/diagram-unquoted.png');
  });

  it('uses a placeholder only while an enabled authorized resolver is pending', () => {
    const text = '![chart](assets/chart.png)';
    expect(rewriteLocalImageSources(text, {
      enabled: true,
      resolvedImages: new Map(),
      unavailableText: 'unavailable',
    })).toContain(IMG_PLACEHOLDER);

    expect(rewriteLocalImageSources(text, {
      enabled: false,
      resolvedImages: new Map(),
      unavailableText: 'unavailable',
    })).toBe(text);
  });
});
