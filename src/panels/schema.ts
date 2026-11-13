import { escapeHtml } from "./html.js";

export type SchemaColumn = {
  name: string;
  type: string;
  notNull: boolean;
  default?: string | null;
};

export type SchemaConstraint = {
  name: string;
  type: string;
  definition: string;
};

export type SchemaTable = {
  name: string;
  columns: SchemaColumn[];
  primaryKey: string[];
  constraints: SchemaConstraint[];
};

export type SchemaRelationship = {
  child: string;
  parent: string;
  childColumns: string[];
  parentColumns: string[];
  required: boolean;
  unique: boolean;
  label?: string;
};

export type SchemaInput = {
  schema: string;
  erdText: string;
  tables: SchemaTable[];
  relationships: SchemaRelationship[];
  differences: Array<{ message: string }>;
};

export type SchemaView = {
  liveDiagram: string;
  relationships: string[];
  constraints: string[];
  differences: string[];
};

function diagramType(type: string): string {
  return type.replace(/[^A-Za-z0-9_]/g, "_");
}

function relationshipWords(relationship: SchemaRelationship): string {
  const parentCount = relationship.unique ? "zero or one" : "zero or many";
  const childCount = relationship.required ? "exactly one" : "zero or one";
  return `${relationship.child}: ${childCount} ${relationship.parent}. ${relationship.parent}: ${parentCount} ${relationship.child}.`;
}

function relationshipLine(relationship: SchemaRelationship): string {
  const parentMark = relationship.required ? "||" : "|o";
  const childMark = relationship.unique ? "o|" : "o{";
  const label = relationship.label ?? `fk_${relationship.child}_${relationship.childColumns.join("_")}`;
  return `  ${relationship.parent} ${parentMark}--${childMark} ${relationship.child} : ${label}`;
}

export function buildSchemaView(input: SchemaInput): SchemaView {
  const diagramLines = ["erDiagram"];
  for (const table of input.tables) {
    diagramLines.push(`  ${table.name} {`);
    for (const column of table.columns) {
      const markers = [
        ...(table.primaryKey.includes(column.name) ? ["PK"] : []),
        ...(input.relationships.some(
          (relationship) => relationship.child === table.name && relationship.childColumns.includes(column.name),
        )
          ? ["FK"]
          : []),
      ];
      diagramLines.push(`    ${diagramType(column.type)} ${column.name}${markers.length ? ` ${markers.join(",")}` : ""}`);
    }
    diagramLines.push("  }");
  }
  for (const relationship of input.relationships) {
    diagramLines.push(relationshipLine(relationship));
  }
  const constraints = input.tables.flatMap((table) => [
    ...table.columns
      .filter((column) => column.notNull)
      .map((column) => `${table.name}.${column.name}: NOT NULL`),
    ...table.constraints.map((constraint) => `${table.name}.${constraint.name}: ${constraint.definition}`),
  ]);
  return {
    liveDiagram: diagramLines.join("\n"),
    relationships: input.relationships.map(relationshipWords),
    constraints,
    differences: input.differences.map((difference) => difference.message),
  };
}

export function renderSchema(input: SchemaInput): string {
  const view = buildSchemaView(input);
  const erd = input.erdText || "No Mermaid erDiagram block appears in README.md.";
  const differenceList = view.differences.map((difference) => `<li>${escapeHtml(difference)}</li>`).join("");
  const relationshipList = view.relationships.map((relationship) => `<li>${escapeHtml(relationship)}</li>`).join("");
  const constraintList = view.constraints.map((constraint) => `<li>${escapeHtml(constraint)}</li>`).join("");
  return `<section><h3>ERD from README.md</h3><pre class="mermaid">${escapeHtml(erd)}</pre></section><section><h3>Live schema</h3><pre class="mermaid">${escapeHtml(view.liveDiagram)}</pre></section><section><h3>Differences</h3><ul>${differenceList || "<li>No differences found.</li>"}</ul></section><section><h3>Relationships</h3><ul>${relationshipList || "<li>No foreign keys found.</li>"}</ul></section><section><h3>Columns and constraints</h3><ul>${constraintList || "<li>No constraints found.</li>"}</ul></section>`;
}
