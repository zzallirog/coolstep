// instrument.js — coolstep dashboard runtime.
//
// Naming: the dashboard is staged as a working instrument (see styles.css
// design notes). This file is its driver — the place where every pill
// module is registered and where the i18n DOM walker hooks the language
// store. Anything *displayed* in the masthead has a sibling under pills/
// that owns its endpoint, state, and paint. Anything *displayed* below
// the masthead is a Lit custom element under components/. Two clean
// authorities, no shared mutation.
//
// Adding a new pill:
//   1. write pills/<name>.js exporting `start({ pollMs })`
//   2. add the import + call below
//   3. add the markup with id="pill-<name>" to index.html
//   4. add data-i18n keys to i18n/strings.js
// No central registry to edit, no module discovers another. Each pill is a
// closed cell.

import { LangStore } from './i18n/lang-store.js';
import { applyI18nToDom } from './i18n/dom-walker.js';

import { start as startHealth } from './pills/health.js';
import { start as startCoverage } from './pills/coverage.js';
import { start as startSignals } from './pills/signals.js';
import { start as startActuators } from './pills/actuators.js';
import { start as startDrift } from './pills/drift.js';
import { start as startStress } from './pills/stress.js';
import { start as startCrash } from './pills/crash.js';
import { start as startProfile } from './pills/profile.js';

function boot() {
  // i18n first: paint static markup before any pill writes over it.
  applyI18nToDom();
  LangStore.subscribe(() => applyI18nToDom());

  // Each pill carries its own cadence — the masthead has no global tick.
  startHealth({ pollMs: 10000 });
  startCoverage({ pollMs: 30000 });
  startSignals({ pollMs: 60000 });
  startActuators({ pollMs: 30000 });
  startDrift({ pollMs: 60000 });
  startStress({ pollMs: 5000 });
  startCrash({ pollMs: 60000 });
  startProfile({ pollMs: 5000 });
}

boot();
