import http from 'k6/http';
import { check } from 'k6';

const BASE     = __ENV.TARGET_URL || 'http://127.0.0.1:18080';
const N        = __ENV.WORK_N     || '3000';
const RATE     = parseInt(__ENV.RATE || '50', 10);
const DURATION = __ENV.DURATION   || '45s';
const PREVUS   = parseInt(__ENV.PREALLOC_VUS || '200', 10);

export const options = {
  scenarios: {
    capacity: {
      executor: 'constant-arrival-rate',
      rate: RATE,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: PREVUS,
      maxVUs: PREVUS * 4,
    },
  },
};

export default function () {
  const r = http.get(`${BASE}/work?n=${N}`);
  check(r, { 'status 200': (res) => res.status === 200 });
}
