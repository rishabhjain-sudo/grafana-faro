// Where the browser sends Faro data. Alloy's faro.receiver is published on the
// host at :12347, so a browser on the same machine can reach it.
window.FARO_CONFIG = {
  collectorUrl: 'http://localhost:12347/collect',
  app: { name: 'demo-shop', version: '1.0.0', environment: 'poc' },
};
