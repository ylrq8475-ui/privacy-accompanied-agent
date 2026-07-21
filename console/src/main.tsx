import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App";
import "../tokens.css";
import "./styles.css";
import "./cinder-console.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
