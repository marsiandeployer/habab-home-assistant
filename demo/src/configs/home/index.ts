import type { DemoConfig } from "../types";
import { demoAreasHome, demoFloorsHome } from "./areas";
import { demoEntitiesHome } from "./entities";
import { demoLovelaceHome } from "./lovelace";

export const demoHome: DemoConfig = {
  authorName: "Home Assistant",
  authorUrl: "https://www.home-assistant.io",
  name: "Мой дом",
  description:
    "Обзор дома, автоматически собранный по комнатам и устройствам.",
  lovelace: demoLovelaceHome,
  entities: demoEntitiesHome,
  floors: demoFloorsHome,
  areas: demoAreasHome,
  theme: { theme: "default", dark: false },
};
