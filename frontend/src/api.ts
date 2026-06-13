export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, options);

  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      message = body.error || message;
    } catch {
      // keep HTTP message
    }
    throw new Error(message);
  }

  return response.json() as Promise<T>;
}
