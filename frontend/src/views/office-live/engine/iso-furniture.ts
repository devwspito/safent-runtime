/** Furniture sprite mapping — connects FurnitureType to pixel-art SpriteData */

import type { IsoFurnitureType, SpriteData } from "./types"

import {
  BOOKSHELF_SPRITE,
  CHAIR_VARIANTS,
  COFFEE_MACHINE_SPRITE,
  DESK_VARIANTS,
  LAMP_VARIANTS,
  LAPTOP_VARIANTS,
  PAINTING_VARIANTS,
  PHONE_SPRITE,
  PLANT_VARIANTS,
  PRINTER_SPRITE,
  ROUTER_SPRITE,
  SIDE_TABLE_SPRITE,
  TOOLBOX_SPRITE,
  TV_SPRITE,
  WATER_COOLER_SPRITE,
  WHITEBOARD_SPRITE,
} from "./sprites"

// Z-depth sub-offsets per furniture type (for sorting within same row)
export const FURNITURE_Z_OFFSETS: Record<IsoFurnitureType, number> = {
  chair: 0.03,
  desk: 0.05,
  laptop: 0.08,
  phone: 0.08,
  plant: 0.1,
  bookshelf: 0.1,
  whiteboard: 0.02,
  tv: 0.02,
  coffee: 0.1,
  printer: 0.1,
  router: 0.1,
  cooler: 0.1,
  lamp: 0.1,
  painting: 0.02,
  toolbox: 0.1,
  sidetable: -0.05,
  emptydesk: 0.05,
}

const SPRITE_MAP: Record<IsoFurnitureType, SpriteData[]> = {
  desk: DESK_VARIANTS,
  chair: CHAIR_VARIANTS,
  laptop: LAPTOP_VARIANTS,
  phone: [PHONE_SPRITE],
  plant: PLANT_VARIANTS,
  bookshelf: [BOOKSHELF_SPRITE],
  whiteboard: [WHITEBOARD_SPRITE],
  tv: [TV_SPRITE],
  coffee: [COFFEE_MACHINE_SPRITE],
  printer: [PRINTER_SPRITE],
  router: [ROUTER_SPRITE],
  cooler: [WATER_COOLER_SPRITE],
  lamp: LAMP_VARIANTS,
  painting: PAINTING_VARIANTS,
  toolbox: [TOOLBOX_SPRITE],
  sidetable: [SIDE_TABLE_SPRITE],
  emptydesk: DESK_VARIANTS,
}

/** Get the SpriteData for a furniture type and variant */
export function getFurnitureSprite(
  type: IsoFurnitureType,
  variant: number
): SpriteData {
  const variants = SPRITE_MAP[type]
  return variants[variant % variants.length]!
}

// Legacy export for renderer compatibility
export const CHAIR_SEAT_H = 0
