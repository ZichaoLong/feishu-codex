// Product-neutral authenticated-media adapter for Kimi-derived rendering
// components. Transport ownership stays with the product entry point: Focus
// supplies its same-origin attachment loader explicitly, and this shared
// rendering layer never discovers or instantiates a backend client.

export type AuthenticatedMediaLoader = (fileId: string) => Promise<Blob>;

export function loadAuthenticatedMediaBlob(
  fileId: string,
  loadBlob?: AuthenticatedMediaLoader,
): Promise<Blob> {
  if (!loadBlob) {
    return Promise.reject(new Error('Authenticated media requires an explicit byte loader.'));
  }
  return loadBlob(fileId);
}
