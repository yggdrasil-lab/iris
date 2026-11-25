import React from 'react';
import ReactDOM from 'react-dom/client';
import { initGlobalErrorReporter } from '@yggdrasil-lab/ui';
import '@yggdrasil-lab/ui/style.css';
import './index.css';
import { App } from './App';

initGlobalErrorReporter();

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
