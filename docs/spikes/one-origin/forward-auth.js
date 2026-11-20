// Spike: n8n accepts an identity a proxy asserts (X-Forwarded-Email) by issuing its own session cookie.
// Loaded through EXTERNAL_HOOK_FILES; runs on n8n.ready with the server (its Express app) as argument.
const N8N = '/usr/local/lib/node_modules/n8n/';
module.exports = {
  n8n: {
    ready: [async function (server) {
      const { Container } = require(N8N + 'node_modules/@n8n/di');
      const { AuthService } = require(N8N + 'dist/auth/auth.service.js');
      const auth = Container.get(AuthService);
      const users = this.dbCollections.User;
      server.app.use(async (req, res, next) => {
        try {
          const email = req.headers['x-forwarded-email'];
          if (email && !/(^|;\s*)n8n-auth=/.test(req.headers.cookie || '')) {
            const user = await users.findOne({ where: { email }, relations: ['role'] });
            if (user) auth.issueCookie(res, user, false, undefined);
            console.log('[forward-auth]', email, user ? 'cookie issued' : 'no such user');
          }
        } catch (e) { console.log('[forward-auth] error', e.message); }
        next();
      });
      const stack = server.app.router.stack;  // Express 5: move the layer before every route
      stack.unshift(stack.pop());
      console.log('[forward-auth] installed, layers:', stack.length);
    }],
  },
};
