self.addEventListener('push', (event) => {
	let payload = {};
	try {
		payload = event.data?.json() ?? {};
	} catch {
		payload = { body: event.data?.text() ?? '' };
	}

	const title = payload.title || 'Open WebUI';
	const options = {
		body: payload.body || '',
		icon: payload.icon || '/static/favicon.png',
		badge: '/static/favicon.png',
		tag: payload.tag || 'open-webui',
		data: {
			url: payload.url || '/'
		}
	};

	event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
	event.notification.close();

	let targetUrl = new URL(event.notification.data?.url || '/', self.location.origin);
	if (targetUrl.origin !== self.location.origin) {
		targetUrl = new URL('/', self.location.origin);
	}

	event.waitUntil(
		clients.matchAll({ type: 'window', includeUncontrolled: true }).then(async (windowClients) => {
			for (const client of windowClients) {
				if ('navigate' in client) {
					await client.navigate(targetUrl.href);
				}
				if ('focus' in client) {
					return client.focus();
				}
			}

			return clients.openWindow(targetUrl.href);
		})
	);
});
