import http from 'k6/http';
import { check, sleep } from 'k6';

export const options = {
  stages: [
    { duration: '3m',  target: 20  },
    { duration: '30s', target: 100 },
    { duration: '2m',  target: 100 },
    { duration: '30s', target: 20  },
    { duration: '3m',  target: 20  },
    { duration: '30s', target: 0   },
  ],
  thresholds: {
    http_req_failed: ['rate<0.10'],
  },
};

const BASE = __ENV.TARGET_URL || 'http://workload.workload.svc';
const N    = __ENV.WORK_N || '3000';

export default function () {
  const r = http.get(`${BASE}/work?n=${N}`);
  check(r, { 'status 200': (res) => res.status === 200 });
  sleep(0.1);
}
