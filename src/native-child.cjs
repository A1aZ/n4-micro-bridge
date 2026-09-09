'use strict';

// These options affect only the child launched by this WebUI, never the
// user's global Python/shell configuration.
function nativeSpawnOptions(cwd, env = process.env) {
  return {
    cwd, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
    env: {...env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1'},
  };
}

function observeNativeChild(child, {isCurrent, getControl, onRunning, onStdout, onStderr}) {
  // setEncoding uses a streaming decoder: a Chinese character split across
  // two pipe chunks must not become two replacement characters.
  child.stdout?.setEncoding('utf8');
  child.stderr?.setEncoding('utf8');
  child.stdout?.on('data', onStdout);
  child.stderr?.on('data', onStderr);
  child.once('spawn', () => {
    const control = getControl();
    if (!isCurrent() || !control.managed || control.status !== 'starting' || control.stopRequested) return;
    // Spawn is evidence of a process, not of an open or ready USB device.
    onRunning();
  });
}

module.exports = {nativeSpawnOptions, observeNativeChild};
